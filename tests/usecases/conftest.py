"""Shared by the use cases under ``tests/usecases``: the signed-in run
(``live``), Node (``node``), an SDK's driver behind its own recording proxy
(``proxied``), and the helpers the cases poll and wait with (``_poll``,
``_wait_until``, ``_synced``). The cases import the helpers from here
(``from conftest import ...``); the fixtures pytest finds itself.

Nothing here skips: no Node, an old Node or no ``sdk/js/dist`` fails.

Environment: ``CONTROLVAR_API_URL`` (default ``http://127.0.0.1:8000``);
``FIRST_SUPERUSER`` and ``FIRST_SUPERUSER_PASSWORD`` (default: the first
administrator the bootstrap creates on an empty development database).
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
import pytest
from usecases import admin, gates, proxy, runner, sdks

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
        reporter = config.pluginmanager.get_plugin("terminalreporter")
        if reporter is not None:
            reporter.flush()


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


@contextmanager
def proxied(
    live: Live,
    sdk: str,
    node: sdks.Node | None,
    *,
    mode: str,
    expect_ready: bool = True,
    fault: proxy.Fault | None = None,
    **options: Any,
) -> Iterator[tuple[runner.Runner, proxy.RecordingProxy]]:
    """*sdk*'s driver behind its own proxy, its identity checked, initialised
    in *mode*. In local mode ``init`` must answer ``ready`` *expect_ready*:
    true unless the case starts the driver while *fault* keeps its first
    ruleset away. *fault*, when given, is injected before ``init``; the case
    clears it.

    On the way out, once the driver has stopped, a case that injected any
    fault is checked by ``proxy.teardown_problem``: no fault left on, and one
    ruleset ``GET`` through the proxy recorded unfaulted, 200 or 304."""
    with proxy.RecordingProxy(live.api_url) as relay:
        if fault is not None:
            relay.inject(fault)
        with sdks.driver(sdk, node=node, secrets=(live.api_key,)) as (session, _):
            answer = session.call(
                "init", api_url=relay.origin, api_key=live.api_key, mode=mode, **options
            )
            if answer.get("ok") is not True:
                pytest.fail(
                    f"the {sdk} driver refused init: {answer.get('error')}",
                    pytrace=False,
                )
            if mode == "local" and answer.get("ready") is not expect_ready:
                if expect_ready:
                    pytest.fail(
                        f"the {sdk} driver loaded no ruleset within 5 s in local mode",
                        pytrace=False,
                    )
                pytest.fail(
                    f"the {sdk} driver loaded a ruleset in local mode, expected none"
                    f" while {relay.active!r} was injected",
                    pytrace=False,
                )
            yield session, relay
        if relay.changes():
            problem = proxy.teardown_problem(relay, {"X-API-Key": live.api_key})
            if problem is not None:
                pytest.fail(
                    f"the {sdk} driver's proxy at teardown: {problem}", pytrace=False
                )


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


def _synced(
    session: runner.Runner,
    relay: proxy.RecordingProxy,
    version: str,
    label: str,
    *,
    caller: str,
    deadline: float,
) -> None:
    """Wait until *version*'s ruleset went through the proxy as a relayed
    200 and the driver's ``status()`` says it is the one in use. Fails, naming
    *caller*, at *deadline* (on ``time.monotonic``): G1-1b gives 3R from the
    call; a case that faulted the refreshes gives the last faulted request's
    answer plus the backoff step after it plus R."""
    wanted = gates.etag_of(version)
    started = time.monotonic()
    while True:
        status = session.call("status")
        through = any(
            r.path == proxy.RULESET_PATH
            and r.status == 200
            and r.fault is None
            and r.etag == wanted
            for r in relay.records()
        )
        if through and gates.etag_of(status.get("ruleset_version")) == wanted:
            return
        if time.monotonic() >= deadline:
            pytest.fail(
                f"{caller} failed: the ruleset {wanted} did not reach {label} through"
                f" its proxy within {deadline - started:.0f} s (through the proxy:"
                f" {through}; status() says {status.get('ruleset_version')!r})",
                pytrace=False,
            )
        time.sleep(SYNC_POLL)
