"""Use case #4 of D210 over the wire: flags evaluated in the request path,
locally, and still answered when the API is not there. This checkout's Python
and JavaScript SDKs in local mode, against a running Controlvar
(``_platform.yml``'s ``usecases-flags`` suite, nightly).

Every case drives both SDKs, each in its own driver behind its own recording
proxy, and puts the same fault on both proxies (``usecases/proxy.py``: a 503
on every path, a corrupt ruleset, a ruleset request held unanswered). The
proxy tags each request it answered with a fault, so the gates
(``usecases/gates.py``, checked against synthetic traces in the unit job)
count faults from its record, never from timestamps. Every case refreshes
every R = 5 s (the SDKs' minimum) and states its cache lifetime: a local
answer is cached too, so a long cache can hide a discarded ruleset from the
answers, and only ``status()`` and the ETag then show it.

* **G4-1, an API outage** (cache 0 s). Ten local flags (nine at 0 to 100%,
  one at 100% to turn off later), 100 users: 1,000 pairs. The API answers 503
  on every path for 120 s, to both SDKs at once. Every 10 s every pair
  answers what it answered before the outage, locally, and ``status()`` says
  ready on the old ruleset; the SDK retried exactly 5 times, each retry at
  least its backoff step after the last failure (R, 2R, 4R, 8R), and counted
  5 refresh errors; no evaluate request went through the proxy. At the
  restore the last flag is turned off on the API: the same driver, not
  restarted, answers it off from the first ruleset carrying the change, which
  it asks for no sooner than 16R after its 5th failure.
* **G4-2 warm, a corrupt ruleset after a load** (cache 0 s). The ruleset
  route answers a truncated 200 until the proxy has faulted exactly 3
  ruleset requests, then is cleared. Every answer stays the last good one,
  locally; 3 refresh errors, at the backoff; and the first refresh after is a
  304 to a request carrying ``If-None-Match``: the SDK kept its ruleset's
  ETag (a failing gate, never a report).
* **G4-2 cold, a corrupt ruleset at start** (cache 300 s, the default). The
  drivers start while the ruleset route answers a 200 with no flags: they
  load nothing (``ready`` false) and every flag goes to the server, one
  evaluate request per flag and user over two passes (the second from the
  cache). Once cleared, the ruleset loads within the backoff, and 20 new
  users are answered locally for the five rollout flags and by the server
  for the three it evaluates (semver, regex and array rules): exactly one
  request per user for each of those three, none for the others, and
  ``server_evaluated_flags`` names exactly those three (G4-3 over the wire).
* **RES-2 local, a hung API** (cache 0 s, request timeout 3 s). The ruleset
  route answers nothing for up to 10 s. The SDK gives the request up itself,
  and the polls sent and answered while it was held (at least 10) answer as
  before, locally: evaluation never waits on a refresh. The refresh after is
  a 304 to ``If-None-Match``.

Users send no attributes, so O-FLAG (``ground_truth.hashing.flag_bucket``)
predicts every answer, a remote flag's included (its rule does not match and
the global rollout decides). Wall clock is reported in the step summary,
never gated: the gates read the proxy's order, counts and lower bounds only.
``429 == 0`` is a pacing guard. The signed-in run, Node, the proxied drivers
and the polling helpers are ``conftest.py``'s. Nothing here skips.
"""

from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from typing import Any

import pytest
from ground_truth.hashing import flag_bucket
from usecases import gates, proxy, runner, sdks

from conftest import (
    Live,
    _created,
    _fail_on,
    _poll,
    _synced,
    _turned_off,
    _wait_until,
    proxied,
)

#: Every case: the ruleset refresh interval (R; the SDKs' minimum), in
#: seconds, and its longest once jittered (+10%).
REFRESH = 5
REFRESH_MAX = REFRESH * 1.1
#: How often a wait looks at the proxies again.
WAIT_POLL = 0.25

#: G4-1: the cache lifetime (0: a discarded ruleset would send every answer
#: to the server, where the outage answers 503), the outage, how often every
#: pair is polled during it, and the retries the documented backoff allows in
#: it (R, 2R, 4R and 8R after the first: the 6th is due 16R after the 5th).
OUTAGE_TTL = 0
OUTAGE_SECONDS = 120
OUTAGE_ROUND_EVERY = 10
OUTAGE_FAULTS = 5
OUTAGE_PERCENTAGES = (0, 1, 10, 25, 50, 75, 90, 99, 100)
OUTAGE_USERS = 100
#: G4-1 after the restore: the turned-off flag is polled this often, at most
#: this long (the first retry is due 35-42 s after the restore).
KILL_POLL = 0.5
RECOVERY_SECONDS = 60

#: G4-2: the rollout-only (local) flags, and the remote ones: a rule the
#: server evaluates itself (its operator is not one an SDK may evaluate), at
#: a 50% rollout, with the users sending no attributes.
LOCAL_PERCENTAGES = (0, 25, 50, 75, 100)
REMOTE_RULES = {
    "semver": {"attribute": "app_version", "operator": "semver_gte", "value": "2.0.0"},
    "regex": {"attribute": "email", "operator": "regex", "value": "^ops@"},
    "array": {"attribute": "tags", "operator": "array_contains", "value": ["beta"]},
}
REMOTE_PERCENTAGE = 50
G42_USERS = 20
#: G4-2 warm: the cache lifetime (0, as G4-1), the faulted ruleset requests
#: (the proxy is cleared once it has recorded this many), how often every
#: pair is polled.
WARM_TTL = 0
WARM_FAULTS = 3
WARM_ROUND_EVERY = 2.0
#: G4-2 cold: the cache lifetime (the SDKs' default, so the second pass is
#: answered from the cache).
COLD_TTL = 300

#: RES-2 local: the cache lifetime (0, as G4-1), the SDK's request timeout,
#: the proxy's hold (longer than the timeout: the SDK must give up first), the
#: pause between polls, how often status() is read, and the polls the gate
#: wants inside the hold.
HUNG_TTL = 0
HUNG_TIMEOUT_MS = 3000
HUNG_HOLD = 10.0
HUNG_PERCENTAGES = (10, 50, 90)
HUNG_USERS = 20
HUNG_POLL_EVERY = 0.02
HUNG_STATUS_EVERY = 0.5
HUNG_MINIMUM = 10

Pair = gates.Pair
Drivers = Mapping[str, tuple[runner.Runner, proxy.RecordingProxy]]


def users_of(case: str, run: str, count: int) -> list[str]:
    """*count* users in four shapes of id (a counter, a UUID, an e-mail
    address, non-ASCII text), the same for one *case* of one run."""
    rng = random.Random(f"{case}-{run}")
    users = []
    for index in range(count):
        shape = index % 4
        if shape == 0:
            users.append(f"{case}-u-{index}")
        elif shape == 1:
            users.append(str(uuid.UUID(int=rng.getrandbits(128), version=4)))
        elif shape == 2:
            users.append(f"{case}.{index}@example.com")
        else:
            users.append(f"ü-{case}-{index}-ユーザー")
    return users


def _drivers(
    stack: ExitStack, live: Live, node: sdks.Node, **options: Any
) -> dict[str, tuple[runner.Runner, proxy.RecordingProxy]]:
    """Both SDKs' drivers in local mode, refreshing every R, each behind its
    own proxy, until *stack* closes."""
    return {
        sdk: stack.enter_context(
            proxied(
                live,
                sdk,
                node if sdk == "js" else None,
                mode="local",
                refresh_seconds=REFRESH,
                **options,
            )
        )
        for sdk in sdks.SDKS
    }


def _answers(
    session: runner.Runner,
    label: str,
    flags: Mapping[str, int],
    users: Sequence[str],
    *,
    source: str,
) -> tuple[dict[Pair, Any], list[str]]:
    """Every pair's answer, and what is wrong with them: each must be
    O-FLAG's, from *source* (``local`` or ``server``), with no error."""
    answered: dict[Pair, Any] = {}
    findings = []
    for flag, percentage in flags.items():
        seen = []
        for user in users:
            poll = _poll(session, flag, user)
            fine = poll.source == source and poll.error is None
            seen.append(
                (user, poll.enabled if fine else (f"not {source}", poll.source))
            )
            answered[(flag, user)] = poll.enabled
        findings += gates.o_flag_findings(
            f"{label} ({source})", flag, percentage, seen, users, flag_bucket
        )
    return answered, findings


def _round(session: runner.Runner, pairs: Sequence[Pair]) -> dict[Pair, gates.Poll]:
    return {pair: _poll(session, *pair) for pair in pairs}


def _status(session: runner.Runner) -> tuple[float, dict[str, Any]]:
    """``status()``, with the time it was asked for."""
    read = time.monotonic()
    return read, session.call("status")


def _loaded(
    drivers: Drivers, live: Live, case: str, flags: Mapping[str, int], users: list[str]
) -> tuple[str, dict[str, dict[Pair, Any]], list[str]]:
    """The version the server serves, once each driver has it in use; and
    each SDK's answer to every pair (A0), checked against O-FLAG, locally."""
    version = live.admin.ruleset()["version"]
    a0: dict[str, dict[Pair, Any]] = {}
    findings: list[str] = []
    for sdk, (session, relay) in drivers.items():
        _synced(
            session,
            relay,
            version,
            f"the {sdk} driver",
            caller=case,
            deadline=time.monotonic() + 3 * REFRESH,
        )
        a0[sdk], found = _answers(
            session, f"the {sdk} SDK before the fault", flags, users, source="local"
        )
        findings += found
    return version, a0, findings


def _ruleset_requests(records: Sequence[proxy.Record]) -> list[proxy.Record]:
    return sorted(
        (r for r in records if r.path == proxy.RULESET_PATH), key=lambda r: r.seq
    )


def _faulted(records: Sequence[proxy.Record]) -> list[proxy.Record]:
    return [r for r in _ruleset_requests(records) if r.fault is not None]


def _recovered(records: Sequence[proxy.Record]) -> bool:
    """A relayed ruleset request came after the last faulted one."""
    faulted = _faulted(records)
    return bool(faulted) and any(
        r.fault is None and r.seq > faulted[-1].seq for r in _ruleset_requests(records)
    )


def _through_the_fault(
    drivers: Drivers,
    *,
    enough: int,
    since: float,
    first_wait: float,
    case: str,
    tick: Callable[[], None],
    pause: float,
) -> None:
    """With a fault injected on every proxy at *since*: run *tick* (the
    case's polls), pausing *pause* between, until each proxy has faulted
    *enough* ruleset requests (it is then cleared) and relayed one after
    them. Fails, naming *case*, when a proxy's next ruleset request has not
    come by ``gates.retry_deadline`` (the first by *since* + *first_wait*)."""
    cleared: set[str] = set()
    while True:
        waiting = False
        for sdk, (_, relay) in drivers.items():
            records = relay.records()
            faulted = _faulted(records)
            if sdk not in cleared and len(faulted) >= enough:
                relay.clear()
                cleared.add(sdk)
            if sdk in cleared and _recovered(records):
                continue
            waiting = True
            deadline = gates.retry_deadline(
                records, refresh=REFRESH, since=since, first_wait=first_wait
            )
            if time.monotonic() > deadline:
                what = (
                    f"relayed no ruleset request after its {len(faulted)} faulted ones"
                    if sdk in cleared
                    else f"had faulted {len(faulted)} of the {enough} ruleset"
                    " requests the case waits for"
                )
                pytest.fail(
                    f"{case} failed: the {sdk} driver's proxy {what} by"
                    f" {deadline - since:.1f} s after the fault was injected, when"
                    " the documented backoff had the next one due",
                    pytrace=False,
                )
        if not waiting:
            return
        tick()
        time.sleep(pause)


def _found(label: str, findings: list[str]) -> list[str]:
    return [f"{label}: {finding}" for finding in findings]


def _proxy_findings(records: Sequence[proxy.Record], label: str) -> list[str]:
    """What every case gates on every proxy: no 429, no unfaulted 502."""
    return gates.pacing_findings(
        records, f"{label}'s proxy"
    ) + gates.upstream_failure_findings(records, f"{label}'s proxy")


# ---------------------------------------------------------------------------
# G4-1
# ---------------------------------------------------------------------------
def test_g4_1_an_api_outage_keeps_the_last_answers_and_recovers_without_a_restart(
    live: Live, node: sdks.Node
) -> None:
    started = time.monotonic()
    findings: list[str] = []
    outcome = "did not complete"
    try:
        flags = {f"uc-g41-{p}-{live.run}": p for p in OUTAGE_PERCENTAGES}
        kill = f"uc-g41-off-{live.run}"
        flags[kill] = 100
        ids = {
            key: _created(live.admin.create_flag(key, rollout_percentage=p), p)
            for key, p in flags.items()
        }
        users = users_of("g41", live.run, OUTAGE_USERS)
        pairs = [(flag, user) for flag in flags for user in users]
        with ExitStack() as stack:
            drivers = _drivers(stack, live, node, cache_ttl_seconds=OUTAGE_TTL)
            version, a0, findings = _loaded(drivers, live, "G4-1", flags, users)
            t_cut = time.monotonic()
            for _, relay in drivers.values():
                relay.inject(proxy.Fault("status"))
            rounds: dict[str, list[dict[Pair, gates.Poll]]] = {s: [] for s in drivers}
            statuses: dict[str, list[tuple[float, Any]]] = {s: [] for s in drivers}
            step = 1
            while step * OUTAGE_ROUND_EVERY < OUTAGE_SECONDS:
                _wait_until(t_cut + step * OUTAGE_ROUND_EVERY)
                for sdk, (session, _) in drivers.items():
                    rounds[sdk].append(_round(session, pairs))
                    statuses[sdk].append(_status(session))
                step += 1
            _wait_until(t_cut + OUTAGE_SECONDS)
            for _, relay in drivers.values():
                relay.clear()
            t_restore = time.monotonic()
            sentinel = users[0]
            kill_polls = {
                sdk: [_poll(session, kill, sentinel)]
                for sdk, (session, _) in drivers.items()
            }
            toggle_sent = time.monotonic()
            toggled = live.admin.toggle(ids[kill])
            if not _turned_off(toggled):
                pytest.fail(
                    f"G4-1 failed: the toggle left the flag {toggled.get('status')!r}",
                    pytrace=False,
                )
            version_after = live.admin.ruleset().get("version")
            waiting = set(drivers)
            step = 1
            while waiting and step * KILL_POLL <= RECOVERY_SECONDS:
                _wait_until(t_restore + step * KILL_POLL)
                for sdk in sorted(waiting):
                    kill_polls[sdk].append(_poll(drivers[sdk][0], kill, sentinel))
                    if kill_polls[sdk][-1].enabled is False:
                        waiting.discard(sdk)
                step += 1
            after = {
                sdk: session.call("status") for sdk, (session, _) in drivers.items()
            }
            records = {sdk: relay.records() for sdk, (_, relay) in drivers.items()}
        reports = []
        for sdk, recs in records.items():
            label = f"the {sdk} driver"
            findings += gates.last_known_findings(a0[sdk], rounds[sdk], label)
            findings += gates.serving_findings(
                statuses[sdk], recs, label, version=version
            )
            findings += gates.outage_findings(
                recs, label, refresh=REFRESH, expected=OUTAGE_FAULTS
            )
            findings += gates.error_tally_findings(
                after[sdk].get("errors"), recs, label
            )
            findings += gates.local_proxy_findings(recs, label)
            findings += _found(
                label,
                gates.local_kill_findings(
                    recs,
                    version_before=version,
                    toggle_sent=toggle_sent,
                    version_after=version_after,
                    polls=kill_polls[sdk],
                ),
            )
            state = after[sdk]
            if (state.get("ready"), state.get("ruleset_version")) != (
                True,
                version_after,
            ) or state.get("last_error") is not None:
                findings.append(
                    f"{label}: after the restore status() says ready"
                    f" {state.get('ready')!r} on {state.get('ruleset_version')!r}"
                    f" (last error {state.get('last_error')!r}); the server serves"
                    f" {version_after!r}"
                )
            findings += _proxy_findings(recs, label)
            off = [
                p.answered - t_restore for p in kill_polls[sdk] if p.enabled is False
            ]
            flushes = (after[sdk].get("errors") or {}).get("flush")
            reports.append(
                f"{sdk}: {gates.outage_report(recs, t_cut=t_cut, refresh=REFRESH)};"
                f" off {f'{off[0]:.1f} s' if off else 'never'} after the restore;"
                f" flush errors {flushes!r} (reported, not gated)"
            )
        outcome = (
            f"{len(pairs):,} pairs, {len(rounds['python'])} rounds through the"
            f" outage, {len(findings)} findings; {'; '.join(reports)}"
        )
    finally:
        live.summary.append(
            f"G4-1 (local, refresh {REFRESH} s, cache {OUTAGE_TTL} s, 503 on every"
            f" path for {OUTAGE_SECONDS} s): {outcome};"
            f" {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, "G4-1")


# ---------------------------------------------------------------------------
# G4-2 warm
# ---------------------------------------------------------------------------
def test_g4_2_warm_a_corrupt_ruleset_after_a_load_keeps_the_good_one(
    live: Live, node: sdks.Node
) -> None:
    started = time.monotonic()
    findings: list[str] = []
    outcome = "did not complete"
    try:
        flags = {f"uc-g42w-{p}-{live.run}": p for p in LOCAL_PERCENTAGES}
        for key, p in flags.items():
            _created(live.admin.create_flag(key, rollout_percentage=p), p)
        users = users_of("g42w", live.run, G42_USERS)
        pairs = [(flag, user) for flag in flags for user in users]
        with ExitStack() as stack:
            drivers = _drivers(stack, live, node, cache_ttl_seconds=WARM_TTL)
            version, a0, findings = _loaded(drivers, live, "G4-2 warm", flags, users)
            rounds: dict[str, list[dict[Pair, gates.Poll]]] = {s: [] for s in drivers}
            statuses: dict[str, list[tuple[float, Any]]] = {s: [] for s in drivers}
            t_inject = time.monotonic()
            due = [t_inject]

            def tick() -> None:
                if time.monotonic() < due[0]:
                    return
                for sdk, (session, _) in drivers.items():
                    rounds[sdk].append(_round(session, pairs))
                    statuses[sdk].append(_status(session))
                due[0] += WARM_ROUND_EVERY

            for _, relay in drivers.values():
                relay.inject(
                    proxy.Fault(
                        "corrupt", paths=(proxy.RULESET_PATH,), body="truncated"
                    )
                )
            _through_the_fault(
                drivers,
                enough=WARM_FAULTS,
                since=t_inject,
                first_wait=REFRESH_MAX + REFRESH,
                case="G4-2 warm",
                tick=tick,
                pause=WAIT_POLL,
            )
            errors = {
                sdk: session.call("status").get("errors")
                for sdk, (session, _) in drivers.items()
            }
            records = {sdk: relay.records() for sdk, (_, relay) in drivers.items()}
        reports = []
        for sdk, recs in records.items():
            label = f"the {sdk} driver"
            findings += gates.outage_findings(
                recs,
                label,
                refresh=REFRESH,
                expected=WARM_FAULTS,
                fault="corrupt:truncated",
            )
            findings += gates.error_tally_findings(errors[sdk], recs, label)
            findings += gates.kept_etag_findings(recs, label)
            findings += gates.last_known_findings(a0[sdk], rounds[sdk], label)
            findings += gates.serving_findings(
                statuses[sdk], recs, label, version=version
            )
            findings += gates.local_proxy_findings(recs, label)
            findings += _proxy_findings(recs, label)
            reports.append(f"{sdk}: {gates.outage_report(recs, t_cut=t_inject)}")
        outcome = (
            f"{len(pairs)} pairs, {len(rounds['python'])} rounds, {len(findings)}"
            f" findings; {'; '.join(reports)}"
        )
    finally:
        live.summary.append(
            f"G4-2 warm (local, refresh {REFRESH} s, cache {WARM_TTL} s, a truncated"
            f" ruleset 200 for {WARM_FAULTS} refreshes): {outcome};"
            f" {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, "G4-2 warm")


# ---------------------------------------------------------------------------
# G4-2 cold
# ---------------------------------------------------------------------------
def test_g4_2_cold_a_corrupt_ruleset_at_start_sends_every_flag_to_the_server(
    live: Live, node: sdks.Node
) -> None:
    started = time.monotonic()
    findings: list[str] = []
    outcome = "did not complete"
    try:
        local = {f"uc-g42c-{p}-{live.run}": p for p in LOCAL_PERCENTAGES}
        remote = {
            f"uc-g42c-{name}-{live.run}": REMOTE_PERCENTAGE for name in REMOTE_RULES
        }
        for key, p in local.items():
            _created(live.admin.create_flag(key, rollout_percentage=p), p)
        for (key, p), rule in zip(remote.items(), REMOTE_RULES.values()):
            targeting = {
                "logical_operator": "AND",
                "groups": [{"logical_operator": "AND", "conditions": [rule]}],
            }
            _created(
                live.admin.create_flag(
                    key, rollout_percentage=p, targeting_rules=targeting
                ),
                p,
            )
        every = {**local, **remote}
        users = users_of("g42c", live.run, G42_USERS)
        new_users = users_of("g42c-new", live.run, G42_USERS)
        corrupt = proxy.Fault(
            "corrupt", paths=(proxy.RULESET_PATH,), body="not-a-ruleset"
        )
        cut: dict[str, int] = {}
        with ExitStack() as stack:
            drivers = {
                sdk: stack.enter_context(
                    proxied(
                        live,
                        sdk,
                        node if sdk == "js" else None,
                        mode="local",
                        expect_ready=False,
                        fault=corrupt,
                        refresh_seconds=REFRESH,
                        cache_ttl_seconds=COLD_TTL,
                    )
                )
                for sdk in sdks.SDKS
            }
            for sdk, (session, _) in drivers.items():
                for number in (1, 2):
                    _, found = _answers(
                        session,
                        f"the {sdk} SDK started cold, pass {number}",
                        every,
                        users,
                        source="server",
                    )
                    findings += found
                state = session.call("status")
                if (state.get("ready"), state.get("ruleset_version")) != (False, None):
                    findings.append(
                        f"the {sdk} driver: before the clear status() says ready"
                        f" {state.get('ready')!r} on {state.get('ruleset_version')!r},"
                        " expected no ruleset"
                    )
            version = live.admin.ruleset()["version"]
            for _, relay in drivers.values():
                relay.clear()
            for sdk, (session, relay) in drivers.items():
                # A refresh the fault answered just before the clear has its
                # record once nothing is in flight.
                relay.wait_idle(5.0)
                _synced(
                    session,
                    relay,
                    version,
                    f"the {sdk} driver",
                    caller="G4-2 cold",
                    deadline=gates.retry_deadline(
                        relay.records(),
                        refresh=REFRESH,
                        since=time.monotonic(),
                        first_wait=REFRESH_MAX + REFRESH,
                    ),
                )
            evaluated = {}
            errors = {}
            for sdk, (session, relay) in drivers.items():
                cut[sdk] = max(r.seq for r in relay.records())
                label = f"the {sdk} SDK once the ruleset loaded"
                _, found = _answers(session, label, local, new_users, source="local")
                findings += found
                _, found = _answers(session, label, remote, new_users, source="server")
                findings += found
                state = session.call("status")
                evaluated[sdk] = sorted(
                    set(state.get("server_evaluated_flags") or ()) & set(every)
                )
                errors[sdk] = state.get("errors")
            records = {sdk: relay.records() for sdk, (_, relay) in drivers.items()}
        reports = []
        for sdk, recs in records.items():
            label = f"the {sdk} driver"
            before = [r for r in recs if r.seq <= cut[sdk]]
            after = [r for r in recs if r.seq > cut[sdk]]
            findings += gates.deferral_findings(
                before,
                f"{label}, two passes with no ruleset",
                remote=dict.fromkeys(every, users),
                local=(),
            )
            findings += gates.deferral_findings(
                after,
                f"{label}, the pass once the ruleset loaded",
                remote=dict.fromkeys(remote, new_users),
                local=local,
            )
            if evaluated[sdk] != sorted(remote):
                findings.append(
                    f"{label}: server_evaluated_flags names {evaluated[sdk]} of this"
                    f" case's flags, expected exactly {sorted(remote)}"
                )
            findings += gates.error_tally_findings(errors[sdk], recs, label)
            findings += _proxy_findings(recs, label)
            calls = sum(1 for r in recs if r.path.startswith(gates.EVALUATE_PREFIX))
            reports.append(
                f"{sdk}: {len(_faulted(recs))} corrupt ruleset answers, {calls}"
                " evaluate requests"
            )
        outcome = (
            f"{len(every)} flags ({len(remote)} remote), {len(users)} users twice"
            f" then {len(new_users)} new, {len(findings)} findings;"
            f" {'; '.join(reports)}"
        )
    finally:
        live.summary.append(
            f"G4-2 cold (local, refresh {REFRESH} s, cache {COLD_TTL} s, a ruleset"
            f" 200 with no flags at start): {outcome};"
            f" {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, "G4-2 cold")


# ---------------------------------------------------------------------------
# RES-2 local
# ---------------------------------------------------------------------------
def _rounds_of(
    polls: Sequence[tuple[Pair, gates.Poll]], size: int
) -> list[dict[Pair, gates.Poll]]:
    """The polls cut into whole passes over the pairs, in the order asked."""
    return [
        dict(polls[start : start + size])
        for start in range(0, len(polls) - size + 1, size)
    ]


def test_res_2_local_a_hung_api_never_stalls_an_evaluation(
    live: Live, node: sdks.Node
) -> None:
    started = time.monotonic()
    findings: list[str] = []
    outcome = "did not complete"
    try:
        flags = {f"uc-res2-{p}-{live.run}": p for p in HUNG_PERCENTAGES}
        for key, p in flags.items():
            _created(live.admin.create_flag(key, rollout_percentage=p), p)
        users = users_of("res2", live.run, HUNG_USERS)
        pairs = [(flag, user) for flag in flags for user in users]
        with ExitStack() as stack:
            drivers = _drivers(
                stack,
                live,
                node,
                cache_ttl_seconds=HUNG_TTL,
                timeout_ms=HUNG_TIMEOUT_MS,
            )
            version, a0, findings = _loaded(drivers, live, "RES-2 local", flags, users)
            polls: dict[str, list[tuple[Pair, gates.Poll]]] = {s: [] for s in drivers}
            statuses: dict[str, list[tuple[float, Any]]] = {s: [] for s in drivers}
            asked = [0]

            def tick() -> None:
                pair = pairs[asked[0] % len(pairs)]
                asked[0] += 1
                for sdk, (session, _) in drivers.items():
                    polls[sdk].append((pair, _poll(session, *pair)))
                    last = statuses[sdk][-1][0] if statuses[sdk] else 0.0
                    if time.monotonic() - last >= HUNG_STATUS_EVERY:
                        statuses[sdk].append(_status(session))

            t_inject = time.monotonic()
            for _, relay in drivers.values():
                relay.inject(
                    proxy.Fault(
                        "blackhole", paths=(proxy.RULESET_PATH,), hold_seconds=HUNG_HOLD
                    )
                )
            _through_the_fault(
                drivers,
                enough=1,
                since=t_inject,
                first_wait=REFRESH_MAX + HUNG_HOLD + REFRESH,
                case="RES-2 local",
                tick=tick,
                pause=HUNG_POLL_EVERY,
            )
            errors = {
                sdk: session.call("status").get("errors")
                for sdk, (session, _) in drivers.items()
            }
            records = {sdk: relay.records() for sdk, (_, relay) in drivers.items()}
        reports = []
        for sdk, recs in records.items():
            label = f"the {sdk} driver"
            findings += gates.hung_refresh_findings(
                recs, a0[sdk], polls[sdk], label, minimum=HUNG_MINIMUM
            )
            findings += gates.last_known_findings(
                a0[sdk], _rounds_of(polls[sdk], len(pairs)), label
            )
            findings += gates.serving_findings(
                statuses[sdk], recs, label, version=version
            )
            findings += gates.error_tally_findings(errors[sdk], recs, label)
            findings += gates.kept_etag_findings(recs, label)
            findings += gates.local_proxy_findings(recs, label)
            findings += _proxy_findings(recs, label)
            held = [r for r in _faulted(recs) if r.fault == "blackhole"]
            inside = (
                sum(
                    1
                    for _, poll in polls[sdk]
                    if held[0].t_recv <= poll.sent and poll.answered <= held[0].t_answer
                )
                if held
                else 0
            )
            reports.append(
                f"{sdk}: held {held[0].t_answer - held[0].t_recv:.2f} s"
                f" (client gave up: {held[0].client_closed}), {inside} polls inside"
                if held
                else f"{sdk}: nothing held"
            )
        outcome = (
            f"{len(pairs)} pairs, {len(polls['python'])} polls an SDK,"
            f" {len(findings)} findings; {'; '.join(reports)}"
        )
    finally:
        live.summary.append(
            f"RES-2 local (local, refresh {REFRESH} s, cache {HUNG_TTL} s, request"
            f" timeout {HUNG_TIMEOUT_MS} ms, the ruleset held up to {HUNG_HOLD:g} s):"
            f" {outcome}; {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, "RES-2 local")
