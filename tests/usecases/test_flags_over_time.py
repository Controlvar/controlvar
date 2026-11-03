"""Use cases #1 and #2 of D210 over the wire: a percentage rollout and the kill
switch, through this checkout's Python and JavaScript SDKs, against a running
Controlvar (``_platform.yml``'s ``usecases-flags`` suite, nightly).

Each SDK runs in its own driver process (``drivers/``) behind its own
recording proxy (``usecases/proxy.py``), so what went over the wire is counted
on the runner's clock. The gates are ``usecases/gates.py``'s, which the unit
job checks against synthetic traces.

* **G1-1b** a fresh flag is ramped 10, 50, 10, 60, 100 and 0 percent by PUT.
  At every step, 2,000 users (the same users each step) are evaluated three
  ways: raw ``GET`` of the evaluate route, and the Python and JS SDKs in local
  mode, each step starting once its ruleset carries the change. Every answer
  is O-FLAG's (``ground_truth.hashing.flag_bucket``), and the local SDKs never
  call the evaluate route.
* **G2-1** a server-mode SDK with a 10 s cache: command 1 answers true; the
  flag is turned off on the API; polls every second answer true from the cache
  until its lifetime ends, and the first call after that asks the API again,
  once, and answers false.
* **G2-2** a local-mode SDK refreshing every 5 s: the flag is turned off on
  the API; polls every half second answer true until the first ruleset with
  the change reaches it, and false from then on, never calling the evaluate
  route.

One user per G2 scenario, so a request to the flag's route is a request for
that user. ``429 == 0`` over every proxy record is a pacing guard, nothing
more. Wall clock is reported in the step summary, never gated. Nothing here
skips: no Node, an old Node or no ``sdk/js/dist`` fails.

Environment: ``CONTROLVAR_API_URL`` (default ``http://127.0.0.1:8000``);
``FIRST_SUPERUSER`` and ``FIRST_SUPERUSER_PASSWORD`` (default: the first
administrator the bootstrap creates on an empty development database).
"""

from __future__ import annotations

import os
import random
import time
import uuid
from collections.abc import Iterator
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from ground_truth.hashing import flag_bucket
from usecases import admin, gates, proxy, runner, sdks

#: G2-1: the server-mode SDKs' cache lifetime, in seconds (C).
CACHE_TTL = 10
#: G1-1b and G2-2: the local-mode SDKs' ruleset refresh interval, in seconds
#: (R; the SDKs' minimum).
REFRESH = 5
#: G1-1b: the percentages the flag is ramped through, by PUT, in order.
RAMP = (10, 50, 10, 60, 100, 0)
#: G1-1b: users evaluated at every step.
USERS_PER_STEP = 2000
#: G2-1 polls this often until the first false, and gives up at t1 + 2C - 1.
REMOTE_POLL = 1.0
#: G2-2 polls this often until the first false, and gives up 3R after the toggle.
LOCAL_POLL = 0.5
#: How often a wait for a driver's ruleset looks again.
SYNC_POLL = 0.25
#: The first administrator ``python -m backend.app.db.bootstrap`` creates on
#: an empty database with ENVIRONMENT=development.
DEV_ADMIN = ("admin@example.com", "admin")
DEFAULT_API_URL = "http://127.0.0.1:8000"
#: The longest wait for one admin request's answer.
ADMIN_TIMEOUT = httpx.Timeout(30.0, connect=10.0)


def _say(config: pytest.Config, line: str) -> None:
    """A line in the run's output, past pytest's capture."""
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    if reporter is not None:
        reporter.write_line(line)


def _mask(config: pytest.Config, value: str | None) -> None:
    """Keep *value* out of a GitHub Actions log."""
    if value and os.environ.get("GITHUB_ACTIONS") == "true":
        _say(config, f"::add-mask::{value}")


@dataclass
class Live:
    """The signed-in run: the API, its key, the admin helper, this run's id
    (in every flag key), and the step summary's lines."""

    api_url: str
    api_key: str
    admin: admin.Admin
    run: str
    summary: list[str] = field(default_factory=list)


def summary_text(live: Live) -> str:
    """The step summary: counts and synthetic ids only."""
    lines = [
        "### Flags over time (D210 use cases 1 and 2)",
        "",
        f"Run {live.run}. Counts and synthetic ids only; wall clock is reported,"
        " never gated.",
        "",
        *(f"- {line}" for line in live.summary),
    ]
    return "\n".join(lines) + "\n"


@pytest.fixture(scope="session")
def live(pytestconfig: pytest.Config) -> Iterator[Live]:
    """Signed in, with an API key carrying ``sdk:ruleset``; both masked in
    the Actions log as they are minted. The step summary at the end."""
    api_url = os.environ.get("CONTROLVAR_API_URL") or DEFAULT_API_URL
    email = os.environ.get("FIRST_SUPERUSER") or DEV_ADMIN[0]
    password = os.environ.get("FIRST_SUPERUSER_PASSWORD") or DEV_ADMIN[1]
    run = f"{random.SystemRandom().randrange(16**8):08x}"
    with httpx.Client(base_url=api_url, timeout=ADMIN_TIMEOUT) as client:
        try:
            token, key = admin.sign_in(
                client,
                email,
                password,
                mask=lambda value: _mask(pytestconfig, value),
            )
        except (admin.AdminError, admin.ground_truth_driver.ReplayError) as exc:
            pytest.fail(
                f"setup failed: signing in at {api_url} or making the API key: {exc}",
                pytrace=False,
            )
        session = Live(api_url, key, admin.Admin(client, token=token, api_key=key), run)
        try:
            yield session
        finally:
            text = summary_text(session)
            for line in text.splitlines():
                _say(pytestconfig, line)
            target = os.environ.get("GITHUB_STEP_SUMMARY")
            if target:
                with open(target, "a", encoding="utf-8") as stream:
                    stream.write(text)


@pytest.fixture(scope="session")
def node() -> sdks.Node:
    """Node 18 or later, and this checkout's built ``sdk/js``: fails, never
    skips, without them."""
    found = sdks.require_node(os.environ.get("PATH", ""))
    sdks.require_dist()
    return found


def _node_for(request: pytest.FixtureRequest, sdk: str) -> sdks.Node | None:
    return request.getfixturevalue("node") if sdk == "js" else None


@contextmanager
def proxied(
    live: Live, sdk: str, node: sdks.Node | None, *, mode: str, **options: Any
) -> Iterator[tuple[runner.Runner, proxy.RecordingProxy]]:
    """*sdk*'s driver behind its own proxy, its identity checked, initialised
    in *mode*; in local mode, ready."""
    with (
        proxy.RecordingProxy(live.api_url) as relay,
        sdks.driver(sdk, node=node, secrets=(live.api_key,)) as (session, _),
    ):
        answer = session.call(
            "init", api_url=relay.origin, api_key=live.api_key, mode=mode, **options
        )
        if answer.get("ok") is not True:
            pytest.fail(
                f"the {sdk} driver refused init: {answer.get('error')}", pytrace=False
            )
        if mode == "local" and answer.get("ready") is not True:
            pytest.fail(
                f"the {sdk} driver loaded no ruleset within 5 s in local mode",
                pytrace=False,
            )
        yield session, relay


def _poll(session: runner.Runner, flag: str, user: str) -> gates.Poll:
    sent = time.monotonic()
    answer = session.call("evaluate", flag=flag, user=user, default=None)
    answered = time.monotonic()
    return gates.Poll(
        sent=sent,
        answered=answered,
        enabled=answer.get("enabled"),
        source=answer.get("source"),
        reason=answer.get("reason"),
        error=answer.get("error"),
    )


def _wait_until(target: float) -> None:
    pause = target - time.monotonic()
    if pause > 0:
        time.sleep(pause)


def _created(flag: dict[str, Any], percentage: int) -> str:
    if (flag.get("status"), flag.get("rollout_percentage")) != ("active", percentage):
        pytest.fail(
            f"the flag was not created active at {percentage}%: status"
            f" {flag.get('status')!r}, {flag.get('rollout_percentage')!r}%",
            pytrace=False,
        )
    return str(flag["id"])


def _fail_on(findings: list[str], case: str) -> None:
    if findings:
        pytest.fail(f"{case} failed:\n  " + "\n  ".join(findings), pytrace=False)


def ramp_users(run: str) -> list[str]:
    """G1-1b's users: four shapes of id (a counter, a UUID, an e-mail address,
    non-ASCII text), the same for every step of one run."""
    rng = random.Random(f"g1-1b-{run}")
    users = []
    for index in range(USERS_PER_STEP):
        shape = index % 4
        if shape == 0:
            users.append(f"u-{index}")
        elif shape == 1:
            users.append(str(uuid.UUID(int=rng.getrandbits(128), version=4)))
        elif shape == 2:
            users.append(f"user.{index}@example.com")
        else:
            users.append(f"ü-{index}-ユーザー")
    return users


def _synced(
    session: runner.Runner, relay: proxy.RecordingProxy, version: str, label: str
) -> None:
    """Wait (at most 3R) until *version*'s ruleset went through the proxy as
    a 200 and the driver's ``status()`` says it is the one in use."""
    wanted = gates.etag_of(version)
    deadline = time.monotonic() + 3 * REFRESH
    while True:
        status = session.call("status")
        through = any(
            r.path == proxy.RULESET_PATH and r.status == 200 and r.etag == wanted
            for r in relay.records()
        )
        if through and gates.etag_of(status.get("ruleset_version")) == wanted:
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"G1-1b failed: the ruleset {wanted} did not reach {label} through its"
                f" proxy within {3 * REFRESH} s (through the proxy: {through};"
                f" status() says {status.get('ruleset_version')!r})",
                pytrace=False,
            )
        time.sleep(SYNC_POLL)


def _local_answers(
    session: runner.Runner, flag: str, users: list[str]
) -> list[tuple[str, Any]]:
    """Each user's answer from a local-mode driver: ``enabled`` when it was
    answered locally without an error, else a marker no gate takes for a
    boolean."""
    answers: list[tuple[str, Any]] = []
    for user in users:
        answer = session.call("evaluate", flag=flag, user=user, default=None)
        local = answer.get("source") == "local" and answer.get("error") is None
        enabled = (
            answer.get("enabled") if local else ("not local", answer.get("source"))
        )
        answers.append((user, enabled))
    return answers


def _evaluate_calls(records: list[proxy.Record]) -> int:
    return sum(1 for r in records if r.path.startswith(gates.EVALUATE_PREFIX))


# ---------------------------------------------------------------------------
# G1-1b
# ---------------------------------------------------------------------------
def test_g1_1b_a_ramped_flag_answers_o_flag_for_every_user_over_the_wire(
    live: Live, node: sdks.Node
) -> None:
    started = time.monotonic()
    key = f"uc-g11b-{live.run}"
    users = ramp_users(live.run)
    findings: list[str] = []
    compared = 0
    outcome = "did not complete"
    try:
        flag_id = _created(live.admin.create_flag(key, rollout_percentage=0), 0)
        with ExitStack() as stack:
            drivers = {}
            for sdk in sdks.SDKS:
                drivers[sdk] = stack.enter_context(
                    proxied(
                        live,
                        sdk,
                        node if sdk == "js" else None,
                        mode="local",
                        refresh_seconds=REFRESH,
                    )
                )
            for percentage in RAMP:
                changed = live.admin.set_rollout(flag_id, percentage)
                if (changed.get("status"), changed.get("rollout_percentage")) != (
                    "active",
                    percentage,
                ):
                    pytest.fail(
                        f"G1-1b failed: after the PUT to {percentage}% the flag is"
                        f" {changed.get('status')!r} at"
                        f" {changed.get('rollout_percentage')!r}%",
                        pytrace=False,
                    )
                version = live.admin.ruleset()["version"]
                raw = [
                    (user, live.admin.evaluate(key, user).get("enabled"))
                    for user in users
                ]
                findings += gates.o_flag_findings(
                    "raw evaluate", key, percentage, raw, users, flag_bucket
                )
                compared += len(raw)
                for sdk, (session, relay) in drivers.items():
                    _synced(session, relay, version, f"the {sdk} driver")
                    answers = _local_answers(session, key, users)
                    findings += gates.o_flag_findings(
                        f"the {sdk} SDK in local mode",
                        key,
                        percentage,
                        answers,
                        users,
                        flag_bucket,
                    )
                    compared += len(answers)
            proxied_counts = []
            for sdk, (_, relay) in drivers.items():
                records = relay.records()
                findings += gates.local_proxy_findings(records, f"the {sdk} driver")
                findings += gates.pacing_findings(records, f"the {sdk} driver's proxy")
                rulesets = sum(1 for r in records if r.path == proxy.RULESET_PATH)
                proxied_counts.append(
                    f"{sdk} {rulesets} ruleset requests and"
                    f" {_evaluate_calls(records)} evaluate calls"
                )
        outcome = (
            f"{compared:,} answers compared, {len(findings)} findings;"
            f" proxies: {', '.join(proxied_counts)}"
        )
    finally:
        live.summary.append(
            f"G1-1b: ramp {'/'.join(map(str, RAMP))}%, {USERS_PER_STEP:,} users a step,"
            f" raw + Python local + JS local: {outcome}; {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, "G1-1b")


# ---------------------------------------------------------------------------
# G2-1
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sdk", sdks.SDKS)
def test_g2_1_a_flag_turned_off_reaches_a_server_mode_sdk_when_its_cache_ends(
    live: Live, request: pytest.FixtureRequest, sdk: str
) -> None:
    started = time.monotonic()
    key = f"uc-g21-{sdk}-{live.run}"
    user = f"uc-g21-user-{live.run}"
    route = admin.EVALUATE.format(flag_key=key)
    findings: list[str] = []
    outcome = "did not complete"
    try:
        flag_id = _created(live.admin.create_flag(key, rollout_percentage=100), 100)
        with proxied(
            live,
            sdk,
            _node_for(request, sdk),
            mode="server",
            cache_ttl_seconds=CACHE_TTL,
        ) as (session, relay):
            first = _poll(session, key, user)
            t1, a1 = first.sent, first.answered
            if first.enabled is not True or first.error is not None:
                pytest.fail(
                    f"G2-1 failed ({sdk}): command 1 answered {first.enabled!r}"
                    f" (error {first.error!r}), expected true",
                    pytrace=False,
                )
            toggled = live.admin.toggle(flag_id)
            server = live.admin.evaluate(key, user)
            if toggled.get("status") != "inactive" or (
                server.get("enabled"),
                server.get("reason"),
            ) != (False, "inactive"):
                pytest.fail(
                    f"G2-1 failed ({sdk}): the API did not turn the flag off: status"
                    f" {toggled.get('status')!r}, it answers {server.get('enabled')!r}"
                    f" ({server.get('reason')!r})",
                    pytrace=False,
                )
            polls: list[gates.Poll] = []
            step = 1
            while t1 + step * REMOTE_POLL <= t1 + 2 * CACHE_TTL - 1:
                _wait_until(t1 + step * REMOTE_POLL)
                polls.append(_poll(session, key, user))
                if polls[-1].enabled is False:
                    break
                step += 1
            records = relay.records()
        findings += gates.remote_refetch_findings(
            records, route, t1=t1, a1=a1, ttl=CACHE_TTL, polls=polls
        )
        findings += gates.pacing_findings(records, f"the {sdk} driver's proxy")
        on_route = [r for r in records if r.path == route]
        again = [r.t_recv - t1 for r in on_route if r.t_recv >= t1 + CACHE_TTL]
        asked = (
            f"asked again {again[0]:.1f} s after command 1"
            if again
            else "never asked again"
        )
        outcome = (
            f"{len(on_route)} requests to the flag's route; {asked},"
            f" {len(polls)} polls, {len(findings)} findings"
        )
    finally:
        live.summary.append(
            f"G2-1 ({sdk}, server mode, cache {CACHE_TTL} s): {outcome};"
            f" {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, f"G2-1 ({sdk})")


# ---------------------------------------------------------------------------
# G2-2
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("sdk", sdks.SDKS)
def test_g2_2_a_flag_turned_off_reaches_a_local_mode_sdk_with_its_next_ruleset(
    live: Live, request: pytest.FixtureRequest, sdk: str
) -> None:
    started = time.monotonic()
    key = f"uc-g22-{sdk}-{live.run}"
    user = f"uc-g22-user-{live.run}"
    findings: list[str] = []
    outcome = "did not complete"
    try:
        flag_id = _created(live.admin.create_flag(key, rollout_percentage=100), 100)
        with proxied(
            live, sdk, _node_for(request, sdk), mode="local", refresh_seconds=REFRESH
        ) as (session, relay):
            first = _poll(session, key, user)
            if first.enabled is not True or first.source != "local":
                pytest.fail(
                    f"G2-2 failed ({sdk}): before the toggle the flag answered"
                    f" {first.enabled!r} from {first.source!r}, expected true, local",
                    pytrace=False,
                )
            version_before = session.call("status").get("ruleset_version")
            toggle_sent = time.monotonic()
            toggled = live.admin.toggle(flag_id)
            toggle_done = time.monotonic()
            if toggled.get("status") != "inactive":
                pytest.fail(
                    f"G2-2 failed ({sdk}): the toggle left the flag"
                    f" {toggled.get('status')!r}",
                    pytrace=False,
                )
            version_after = live.admin.ruleset().get("version")
            polls = [first]
            step = 1
            while toggle_done + step * LOCAL_POLL <= toggle_done + 3 * REFRESH:
                _wait_until(toggle_done + step * LOCAL_POLL)
                polls.append(_poll(session, key, user))
                if polls[-1].enabled is False:
                    break
                step += 1
            in_use = session.call("status").get("ruleset_version")
            records = relay.records()
        findings += gates.local_kill_findings(
            records,
            version_before=version_before,
            toggle_sent=toggle_sent,
            version_after=version_after,
            polls=polls,
        )
        if polls[-1].enabled is False and in_use != version_after:
            findings.append(
                f"status() says the ruleset in use is {in_use!r}, the server serves"
                f" {version_after!r}"
            )
        findings += gates.pacing_findings(records, f"the {sdk} driver's proxy")
        off = [p.answered - toggle_done for p in polls if p.enabled is False]
        rulesets = sum(1 for r in records if r.path == proxy.RULESET_PATH)
        first_off = (
            f"first false {off[0]:.1f} s after the toggle" if off else "never false"
        )
        outcome = (
            f"{first_off}, {rulesets} ruleset requests and"
            f" {_evaluate_calls(records)} evaluate calls,"
            f" {len(polls)} polls, {len(findings)} findings"
        )
    finally:
        live.summary.append(
            f"G2-2 ({sdk}, local mode, refresh {REFRESH} s): {outcome};"
            f" {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, f"G2-2 ({sdk})")
