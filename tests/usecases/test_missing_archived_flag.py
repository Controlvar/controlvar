"""A flag key the server does not know and an archived flag, over the wire: what
this checkout's Python and JavaScript SDKs answer, in server mode and in local
mode, against a running Controlvar (``_platform.yml``'s ``usecases-flags``
suite, nightly).

Two keys, each asked by its own user with the caller's default set to ``true``,
so an answer that is the default and an answer that is the server's can never
be mistaken for one another:

* an **archived** flag: created active at a global rollout of 100 (a raw
  evaluation first says ``enabled: true, reason: "rollout"``, so the flag was on
  and archiving is what turned it off), then archived through the API
  (``POST /feature-flags/bulk-toggle``, ``"action": "archive"``);
* a **missing** key: no flag has it.

The API's own answers (``docs/feature-flags/create.md``, "Archived flags"): the
archived flag answers 200 ``enabled: false, reason: "inactive"``; the missing
key answers 404.

* **M1, the API.** A raw ``GET`` of the evaluate route answers those two.
* **M2, an archived flag.** Each of the four legs (Python and JavaScript, server
  and local) answers it ``enabled: false, reason: "inactive"``, raises nothing
  (no error) and does not answer the caller's default: the API's answer, to the
  field. A local leg answers it from the ruleset (``source: "local"``).
* **M3, a missing key.** Each leg answers the caller's default (``enabled:
  true``), from ``source: "default"``, with the error the SDK raised carrying
  the API's status (404) and no reason: nothing is made up for a key nobody
  knows.
* **M4, evaluate requests**, counted by the proxy in front of each driver: a
  server leg sends one for each key; a local leg sends none for the archived
  flag (the ruleset answers it) and one for the missing key (the ruleset does
  not know it, so the SDK asks the server), and at least one ruleset 200 went
  through its proxy. A request for any other key fails.
* **M5.** No answer through any proxy is a 429.

Wall clock is reported in the step summary, never gated. The signed-in run,
Node, the proxied drivers and the helpers are ``conftest.py``'s. Nothing here
skips.
"""

from __future__ import annotations

import re
import time
from typing import Any

from usecases import admin, gates, proxy, runner, sdks

from conftest import Live, _created, _fail_on, _synced, proxied

#: The ruleset refresh interval of a local driver (the SDKs' minimum).
REFRESH = 5
#: The caller's default in every question: neither answer the API gives.
DEFAULT = True
RAW = "raw GET"


def _ask(session: runner.Runner, flag: str, user: str) -> gates.Poll:
    """One ``evaluate`` command, with the caller's default ``DEFAULT``."""
    sent = time.monotonic()
    answer = session.call("evaluate", flag=flag, user=user, default=DEFAULT)
    return gates.Poll(
        sent=sent,
        answered=time.monotonic(),
        enabled=answer.get("enabled"),
        source=answer.get("source"),
        reason=answer.get("reason"),
        error=answer.get("error"),
    )


def _status_of(exc: admin.AdminError) -> int | None:
    """The status the helper's error names (``GET <route> answered 404``)."""
    found = re.search(r"answered (\d{3})", str(exc))
    return int(found.group(1)) if found else None


def archived_findings(leg: str, poll: gates.Poll, api: dict[str, Any]) -> list[str]:
    """M2: the archived flag is answered as the API answered it, with no error
    and not as the caller's default."""
    found = []
    if poll.error is not None:
        found.append(f"{leg} archived: raised {poll.error!r}, expected an answer")
    if poll.enabled is not api["enabled"] or poll.reason != api["reason"]:
        found.append(
            f"{leg} archived: answered ({poll.enabled}, {poll.reason}), the API"
            f" says ({api['enabled']}, {api['reason']})"
        )
    if poll.enabled is DEFAULT:
        found.append(f"{leg} archived: answered the caller's default")
    return found


def missing_findings(leg: str, poll: gates.Poll, status: int | None) -> list[str]:
    """M3: the missing key is answered with the caller's default, from
    ``default``, and the error carries the API's status."""
    found = []
    if poll.enabled is not DEFAULT:
        found.append(
            f"{leg} missing: answered enabled {poll.enabled!r}, expected the"
            f" caller's default {DEFAULT!r}"
        )
    if poll.source != "default":
        found.append(f"{leg} missing: source {poll.source!r}, expected 'default'")
    if poll.reason is not None:
        found.append(f"{leg} missing: made up the reason {poll.reason!r}")
    raised = poll.error.get("status") if isinstance(poll.error, dict) else None
    if raised != status:
        found.append(
            f"{leg} missing: the SDK's error carried status {raised!r}, the API"
            f" answered {status!r}"
        )
    return found


def test_a_missing_and_an_archived_flag_answer_as_documented_on_every_leg(
    live: Live, node: sdks.Node
) -> None:
    started = time.monotonic()
    findings: list[str] = []
    outcome = "did not complete"
    try:
        archived_key = f"uc-mia-archived-{live.run}"
        missing_key = f"uc-mia-missing-{live.run}"
        flag = live.admin.create_flag(archived_key, rollout_percentage=100)
        flag_id = _created(flag, 100)

        # The flag is on before it is archived: the control for M2.
        before = live.admin.evaluate(archived_key, f"uc-mia-{live.run}-before")
        if (before.get("enabled"), before.get("reason")) != (True, "rollout"):
            findings.append(
                f"{RAW} before archiving: ({before.get('enabled')},"
                f" {before.get('reason')}), expected (True, rollout)"
            )
        (result,) = live.admin.archive(flag_id)["results"]
        if result.get("success") is not True:
            findings.append(f"archiving the flag failed: {result.get('error')!r}")

        users = {
            archived_key: f"uc-mia-{live.run}-archived",
            missing_key: f"uc-mia-{live.run}-missing",
        }
        api = live.admin.evaluate(archived_key, users[archived_key])
        try:
            live.admin.evaluate(missing_key, users[missing_key])
            missing_status = None
            findings.append(f"{RAW} missing: answered 2xx, expected 404")
        except admin.AdminError as exc:
            missing_status = _status_of(exc)
        if (api.get("enabled"), api.get("reason")) != (False, "inactive"):
            findings.append(
                f"{RAW} archived: ({api.get('enabled')}, {api.get('reason')}),"
                " expected (False, inactive)"
            )
        if missing_status != 404:
            findings.append(f"{RAW} missing: status {missing_status}, expected 404")

        # Read once the flag is archived: the version a local driver must hold.
        version = live.admin.ruleset()["version"]

        records: dict[str, list[proxy.Record]] = {}
        answers: dict[str, dict[str, gates.Poll]] = {}
        for sdk in sdks.SDKS:
            for mode in ("server", "local"):
                leg = f"{sdk} {mode}"
                options: dict[str, Any] = {}
                if mode == "local":
                    options["refresh_seconds"] = REFRESH
                with proxied(
                    live, sdk, node if sdk == "js" else None, mode=mode, **options
                ) as (session, relay):
                    if mode == "local":
                        _synced(
                            session,
                            relay,
                            version,
                            f"the {sdk} local driver (the ruleset holding the archived"
                            " flag)",
                            caller="missing and archived flag (setup)",
                            deadline=time.monotonic() + 3 * REFRESH,
                        )
                    answers[leg] = {
                        key: _ask(session, key, users[key])
                        for key in (archived_key, missing_key)
                    }
                    relay.wait_idle(5.0)
                    records[leg] = relay.records()

        reports = []
        for leg, polls in answers.items():
            local = leg.endswith(" local")
            findings += archived_findings(leg, polls[archived_key], api)
            findings += missing_findings(leg, polls[missing_key], missing_status)
            if local and polls[archived_key].source != "local":
                findings.append(
                    f"{leg} archived: answered from {polls[archived_key].source!r},"
                    " expected 'local'"
                )
            findings += gates.evaluate_count_findings(
                records[leg],
                leg,
                expected={archived_key: 0 if local else 1, missing_key: 1},
                require_ruleset=local,
            )
            findings += gates.pacing_findings(records[leg], f"{leg}'s proxy")
            calls = sum(
                1 for r in records[leg] if r.path.startswith(gates.EVALUATE_PREFIX)
            )
            reports.append(f"{leg} {calls}")
        outcome = (
            f"2 keys on {len(answers)} legs, {len(findings)} findings;"
            f" evaluate requests: {', '.join(reports)}"
        )
    finally:
        live.summary.append(
            f"missing and archived flag (refresh {REFRESH} s): {outcome};"
            f" {time.monotonic() - started:.0f} s"
        )
    _fail_on(findings, "missing and archived flag")


def test_the_checks_catch_a_wrong_answer() -> None:
    """The case's own checks fail on the answers they exist to catch: an
    archived flag answered on, and a missing key answered as a known flag."""
    api = {"enabled": False, "reason": "inactive"}
    right = gates.Poll(0.0, 0.0, False, "server", "inactive", None)
    assert archived_findings("leg", right, api) == []
    on = gates.Poll(0.0, 0.0, True, "server", "rollout", None)
    assert archived_findings("leg", on, api)
    raised = gates.Poll(0.0, 0.0, True, "default", None, {"status": 404})
    assert archived_findings("leg", raised, api)

    default = gates.Poll(0.0, 0.0, DEFAULT, "default", None, {"status": 404})
    assert missing_findings("leg", default, 404) == []
    off = gates.Poll(0.0, 0.0, False, "server", "inactive", None)
    assert len(missing_findings("leg", off, 404)) >= 3
    other = gates.Poll(0.0, 0.0, DEFAULT, "default", None, {"status": 500})
    assert missing_findings("leg", other, 404)
