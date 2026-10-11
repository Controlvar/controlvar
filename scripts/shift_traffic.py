#!/usr/bin/env python3
"""Approve a forward CodeDeploy deployment and wait until the API serves it (#143).

    python3 scripts/shift_traffic.py --deployment-id d-XXXX \\
        --cluster experimentation-<env> --service experimentation-backend-<env> \\
        --task-definition <ARN> --deadline-seconds N --interval-seconds M

This is rollback.yml's polling loop, applied to the forward deploy.

* `Ready` is the deployment group's approval wait: CodeDeploy has started
  the replacement task set and waits up to 30 minutes for ContinueDeployment,
  then stops the deployment. Before approving, every target in the
  replacement task set's target group must be `healthy`, and the count must
  equal the task set's desired count (PE v2 C6(b)). Until they are, it keeps
  polling. Then it sends `continue-deployment --deployment-wait-type
  READY_WAIT`, once. `READY_WAIT` is passed explicitly and never
  `TERMINATION_WAIT` (PE v2 C7).
* `Failed` and `Stopped` end the run red. `Baking` is not terminal. When
  CodeDeploy stopped it for an alarm (`errorInformation.code ==
  ALARM_ACTIVE`, whichever of the two the status is), the result is `alarm`
  and the copy names the alarm. It says the API is going back only when
  CodeDeploy reports a rollback (`rollbackInfo`), and it never advises
  Rollback: CodeDeploy's own rollback is already doing that (#148).
* SUCCESS is `scripts/api_serving.py`'s verdict, checked after every poll:
  the PRIMARY task set is this revision, and check_live_target_group.py
  confirms that the `/api/*` rule forwards to that task set's target group
  (PE v2 C8). A split rule means the shift is still going, so it retries until
  the deadline. "Could not tell" ends the run red. An unsplit rule to the
  other group ends it red too, but only after a bounded grace once the shift
  is approved: an alarm rollback can put the rule back a little before the
  deployment's status says Stopped, and that must end as `alarm`, not
  `wrong-route` (#148 PE condition 5). The grace is `--wrong-route-grace-polls`
  consecutive polls at the usual interval, and the deadline still applies.
* It does not wait for `Succeeded`. After the shift, the deployment group
  keeps the old task set for an hour and the deployment stays active. In that
  hour the deployment group's alarms still watch the API, and CodeDeploy
  rolls it back by itself if one goes into ALARM; this run has ended by then,
  so it cannot report that. It is also the window in which Rollback can act
  (DECISIONS T21).

It NEVER stops a deployment. The only CodeDeploy write it can make is
`continue-deployment`. Two allow-lists cover every AWS call it makes:
`OPERATIONS` below, for its own `run_aws` (the deployment, the replacement
task set, target health), and `check_live_target_group.READ_ONLY_OPERATIONS`,
four describe calls, for the `serving()` path through `api_serving.py`. Each
`run_aws` refuses anything outside its list before a process starts. A deadline passed
before the approval leaves the deployment to CodeDeploy, which stops it when
the approval wait ends; nothing has shifted by then. A deadline passed after
the approval leaves it running. Stopping a deployment whose traffic has
shifted rolls back a release that may be succeeding (#143).

Deadlines and intervals are parameters. The unit tests run with 0.

Exit status: 0 the API is serving the revision; 1 anything else, with the
reason on stdout as a workflow `::error`. An exception it does not expect (a
missing `aws` binary, a malformed rule) is `result=unknown` with its type and
message, not an uncaught traceback with no `result`. When `GITHUB_OUTPUT` is set it
writes `approved=true` as soon as it approves the shift, `result` (serving |
failed | alarm | unhealthy | timeout | wrong-route | unknown), on success
`live_target_group`, `shifted_at` (UTC, when the API was first seen serving)
and `bake_end` (`shifted_at` plus `--bake-minutes`, "YYYY-MM-DD HH:MM" UTC:
roughly when the deployment group stops watching), and on `alarm` the alarm's
name(s) as `alarms` (never empty).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Sequence
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import api_serving
from public_text import redact, short_arn

#: Every AWS CLI operation this script may run. The one write is
#: continue-deployment. A test pins the exact set, so stop-deployment cannot
#: be added without a reviewer seeing it.
OPERATIONS = frozenset(
    {
        ("deploy", "get-deployment"),
        ("deploy", "continue-deployment"),
        ("ecs", "describe-services"),
        ("elbv2", "describe-target-health"),
    }
)

#: Where the wrong-route error sends the operator. One literal, so the docs
#: test that checks every printed anchor exists can see it.
WRONG_ROUTE_DOC = "docs/deployment/deployment-guide.md#the-api-route-and-the-primary-task-set-disagree"

#: Where the alarm copy sends the operator: what to do once CodeDeploy has
#: rolled the API back, and what to do when an alarm is blocking a deploy.
ALARM_DOC = "docs/deployment/rollback-runbook.md#an-alarm-rolled-the-api-back"
ALARM_FIRING_DOC = (
    "docs/deployment/rollback-runbook.md#fix-forward-while-an-alarm-is-firing"
)

#: The statuses after which this deployment will not shift any further.
TERMINAL_FAILURES = ("Failed", "Stopped")

#: errorInformation.code when CodeDeploy stopped a deployment for an alarm.
ALARM_ACTIVE = "ALARM_ACTIVE"

#: Consecutive WRONG verdicts, after the approval, before the run says
#: `wrong-route` (PE condition 5). At the deploy's 10-second interval this is
#: about a minute for the deployment's status to catch up with a rule an alarm
#: rollback has already put back.
WRONG_ROUTE_GRACE_POLLS = 6

#: How much of CodeDeploy's error message the copy quotes.
MESSAGE_LIMIT = 200
# How much of CodeDeploy's message is searched for alarm names; the printed copy
# stays at MESSAGE_LIMIT.
ALARM_SCAN_LIMIT = 4096


class AwsError(Exception):
    """An AWS CLI call failed; the message is its stderr."""


def run_aws(argv: Sequence[str]) -> dict:
    """Run one allowed AWS CLI call and return its JSON output."""
    if tuple(argv[:2]) not in OPERATIONS:
        raise ValueError(f"not an operation this script may run: {argv[:2]}")
    proc = subprocess.run(
        ["aws", *argv, "--output", "json"], capture_output=True, text=True, check=False
    )
    if proc.returncode != 0:
        raise AwsError(proc.stderr.strip() or f"aws exited {proc.returncode}")
    return json.loads(proc.stdout or "{}")


def replacement_ready(
    aws: Callable[[Sequence[str]], dict], cluster: str, service: str, arn: str
) -> tuple[bool, str]:
    """Every target in the replacement task set's group is healthy, count == desired."""
    services = aws(
        ["ecs", "describe-services", "--cluster", cluster, "--services", service]
    ).get("services", [])
    task_sets = [
        t
        for s in services
        for t in s.get("taskSets", [])
        if t.get("taskDefinition") == arn
    ]
    if len(task_sets) != 1:
        return False, f"{len(task_sets)} task sets run {short_arn(arn)}; expected one"
    task_set = task_sets[0]
    desired = int(task_set.get("computedDesiredCount") or 0)
    if desired < 1:
        return False, f"the replacement task set's desired count is {desired}"
    groups = {lb["targetGroupArn"] for lb in task_set.get("loadBalancers", [])}
    if len(groups) != 1:
        return False, f"the replacement task set is in {len(groups)} target groups"
    group = groups.pop()
    targets = aws(["elbv2", "describe-target-health", "--target-group-arn", group]).get(
        "TargetHealthDescriptions", []
    )
    states = Counter(t.get("TargetHealth", {}).get("State", "?") for t in targets)
    summary = ", ".join(f"{n} {state}" for state, n in sorted(states.items()))
    if states["healthy"] == len(targets) == desired:
        return True, f"{desired} of {desired} targets healthy in {short_arn(group)}"
    return False, (
        f"{states['healthy']} of {desired} desired targets healthy in {short_arn(group)} "
        f"({summary or 'no targets registered'})"
    )


def _output(**values: str) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            for key, value in values.items():
                handle.write(f"{key}={value}\n")


def _fail(result: str, title: str, message: str, **outputs: str) -> int:
    print(f"::error title={title}::{redact(message)}")
    _output(result=result, **outputs)
    return 1


def _one_line(text: str) -> str:
    """Truncated first, then flattened: a workflow command and an output are lines."""
    return " ".join(str(text)[:MESSAGE_LIMIT].split())


def alarm_names(message: str, configured: Sequence[str]) -> str:
    """The alarm(s) CodeDeploy's message names, or a fallback; never empty.

    The message's format is not documented, so it is not parsed: a configured
    name is reported when it appears in it. When none does, the copy says it
    was one of the group's alarms and lists them, rather than print an empty
    name.
    """
    found = [name for name in configured if name and name in message]
    if found:
        return ", ".join(found)
    if configured:
        return "one of the deployment group's alarms (" + ", ".join(configured) + ")"
    return "one of the deployment group's alarms"


def _alarm(
    info: dict,
    *,
    deployment_id: str,
    status: str,
    continued: bool,
    alarms: Sequence[str],
    cluster: str,
    service: str,
    arn: str,
) -> int:
    """CodeDeploy stopped this deployment for an alarm (#148 W1)."""
    error = info.get("errorInformation") or {}
    raw = str(error.get("message") or "")
    message = _one_line(raw)
    # The names are looked for in the whole message (bounded, flattened), not in
    # the copy truncated to MESSAGE_LIMIT: CodeDeploy lists every activated alarm,
    # and a second one, or one with a longer name, comes after the first 200
    # characters (the captured staging stop d-F797PV8AL is 240).
    names = alarm_names(" ".join(raw[:ALARM_SCAN_LIMIT].split()), alarms)
    said = f" CodeDeploy said: {message}" if message else ""
    rollback = info.get("rollbackInfo") or {}
    rollback_id = rollback.get("rollbackDeploymentId")
    if not continued:
        return _fail(
            "alarm",
            "Stopped by an alarm",
            f"CodeDeploy stopped deployment {deployment_id} ({status}) before any "
            f"traffic shifted, because {names} is in ALARM. Nothing shifted: the "
            "previous revision keeps serving. While an alarm of this deployment "
            "group is in ALARM, CodeDeploy stops every deployment to it, so find "
            "out why before deploying again: "
            f"{ALARM_FIRING_DOC}.{said}",
            alarms=names,
        )
    if rollback_id or rollback.get("rollbackMessage"):
        going_back = (
            "and its auto-rollback is moving the API back to the previous "
            f"revision (rollback deployment {rollback_id or 'not named'}). "
            "This run stopped nothing. Do not dispatch Rollback for this: "
            "the API is already going back, and Rollback refuses while "
            "CodeDeploy's rollback is active."
        )
    else:
        # No rollback is claimed without CodeDeploy reporting one (P9d).
        going_back = (
            "but CodeDeploy reports no rollback for it (no rollbackInfo), so this "
            "run cannot say the API is going back. This run stopped nothing. "
            "Check what is serving before anything else: python3 "
            f"scripts/api_serving.py {cluster} {service} {short_arn(arn)} (exit 1: this "
            "revision is not serving)."
        )
    return _fail(
        "alarm",
        "Rolled back by an alarm",
        f"CodeDeploy stopped deployment {deployment_id} ({status}) during its "
        f"traffic shift, because {names} went into ALARM, {going_back} "
        f"Next: {ALARM_DOC}.{said}",
        alarms=names,
    )


def shift(
    *,
    deployment_id: str,
    cluster: str,
    service: str,
    arn: str,
    deadline_seconds: float,
    interval_seconds: float,
    aws: Callable[[Sequence[str]], dict] = run_aws,
    serving: Callable[[str, str, str], tuple[int, str, str | None]] | None = None,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    alarms: Sequence[str] = (),
    wrong_route_grace_polls: int = WRONG_ROUTE_GRACE_POLLS,
    bake_minutes: float = 60,
    wall: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> int:
    if serving is None:

        def serving(c: str, s: str, a: str) -> tuple[int, str, str | None]:
            return api_serving.verdict(api_serving.live.run_aws, c, s, a)

    deadline = clock() + deadline_seconds
    continued = False
    last_health = ""
    status = "unknown"
    wrong_polls = 0
    try:
        while True:
            info = aws(
                ["deploy", "get-deployment", "--deployment-id", deployment_id]
            ).get("deploymentInfo", {})
            status = info.get("status", "unknown")

            if (
                status in TERMINAL_FAILURES
                and (info.get("errorInformation") or {}).get("code") == ALARM_ACTIVE
            ):
                return _alarm(
                    info,
                    deployment_id=deployment_id,
                    status=status,
                    continued=continued,
                    alarms=alarms,
                    cluster=cluster,
                    service=service,
                    arn=arn,
                )

            if status in TERMINAL_FAILURES:
                error = json.dumps(info.get("errorInformation") or {})
                after = (
                    "after this run approved the traffic shift; CodeDeploy's "
                    "auto-rollback puts the previous revision back"
                    if continued
                    else "before any traffic shifted; the previous revision "
                    "keeps serving"
                )
                return _fail(
                    "failed",
                    f"Deployment {status}",
                    f"CodeDeploy deployment {deployment_id} is {status} {after}. "
                    f"{error}",
                )

            if status == "Ready" and not continued:
                healthy, why = replacement_ready(aws, cluster, service, arn)
                if healthy:
                    print(f"deployment is Ready and {why}: approving the traffic shift")
                    aws(
                        [
                            "deploy",
                            "continue-deployment",
                            "--deployment-id",
                            deployment_id,
                            "--deployment-wait-type",
                            "READY_WAIT",
                        ]
                    )
                    continued = True
                    # Written the moment the approval is sent: from here on the
                    # new revision may be serving, whatever happens next.
                    _output(approved="true")
                elif why != last_health:
                    print(f"Ready, not yet approved: {why}")
                    last_health = why

            code, sentence, colour = serving(cluster, service, arn)
            sentence = sentence.rstrip(".")
            if code == api_serving.SERVING:
                print(f"serving: {sentence} (deployment {status})")
                shifted = wall()
                _output(
                    result="serving",
                    live_target_group=str(colour),
                    shifted_at=shifted.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    bake_end=(shifted + timedelta(minutes=bake_minutes)).strftime(
                        "%Y-%m-%d %H:%M"
                    ),
                )
                return 0
            if code == api_serving.WRONG:
                wrong_polls += 1
                # An alarm rollback may have put the rule back before the
                # status says so: keep reading the deployment for a bounded
                # grace, and let a terminal status decide (PE condition 5).
                if (
                    continued
                    and wrong_polls < wrong_route_grace_polls
                    and clock() < deadline
                ):
                    print(
                        f"{sentence}; deployment {deployment_id} is {status}: "
                        f"reading it again ({wrong_polls} of "
                        f"{wrong_route_grace_polls - 1} before this counts as a "
                        "wrong route)"
                    )
                    sleep(interval_seconds)
                    continue
                return _fail(
                    "wrong-route",
                    "API route is not on the new revision",
                    f"{sentence}. The deployment was not stopped. First check "
                    f"aws deploy get-deployment --deployment-id {deployment_id}: "
                    "if it is Stopped with ALARM_ACTIVE, CodeDeploy is already "
                    "rolling the API back, so do not dispatch Rollback; read "
                    f"{ALARM_DOC}. Otherwise, roll back with the line in this "
                    f"run's summary, and read {WRONG_ROUTE_DOC} before the next "
                    "cdk deploy.",
                )
            wrong_polls = 0
            if code == api_serving.UNKNOWN:
                return _fail(
                    "unknown",
                    "Could not tell whether the API is serving",
                    f"{sentence}. Deployment {deployment_id} is {status} and was "
                    "not stopped; check it by hand: python3 scripts/api_serving.py "
                    f"{cluster} {service} {short_arn(arn)}",
                )
            if status == "Succeeded":
                return _fail(
                    "failed",
                    "Deployment succeeded, API not serving it",
                    f"CodeDeploy reports {deployment_id} Succeeded, but {sentence}.",
                )

            if clock() >= deadline:
                if continued:
                    return _fail(
                        "timeout",
                        "Traffic shift not confirmed",
                        f"Deployment {deployment_id} was approved and is {status}; "
                        f"after {deadline_seconds:.0f}s {sentence}. It was NOT "
                        "stopped: stopping it now could roll back a release that "
                        "is succeeding. Watch it (aws deploy get-deployment "
                        f"--deployment-id {deployment_id}); if the release is "
                        "bad, use the rollback line in this run's summary.",
                    )
                reason = last_health or f"the deployment is {status}, not Ready"
                return _fail(
                    "unhealthy" if last_health else "timeout",
                    "Traffic shift not approved",
                    f"Deployment {deployment_id} was not approved within "
                    f"{deadline_seconds:.0f}s: {reason}. Nothing has shifted; the "
                    "previous revision keeps serving. CodeDeploy stops the "
                    "deployment when its 30-minute approval wait ends, and the "
                    "next deploy is refused until then.",
                )
            sleep(interval_seconds)
    except (AwsError, ValueError, KeyError) as exc:
        return _fail(
            "unknown",
            "Could not tell whether the API is serving",
            f"{exc}. Deployment {deployment_id} was last {status}; it was not stopped.",
        )
    except Exception as exc:  # any other crash is "could not tell" too
        return _fail(
            "unknown",
            "Could not tell whether the API is serving",
            f"{type(exc).__name__}: {exc}. Deployment {deployment_id} was last "
            f"{status}; it was not stopped.",
        )


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--cluster", required=True)
    parser.add_argument("--service", required=True)
    parser.add_argument("--task-definition", required=True)
    parser.add_argument("--deadline-seconds", type=float, required=True)
    parser.add_argument("--interval-seconds", type=float, required=True)
    parser.add_argument(
        "--alarm",
        action="append",
        default=[],
        help="an alarm of the deployment group, for the copy (repeatable)",
    )
    parser.add_argument(
        "--wrong-route-grace-polls", type=int, default=WRONG_ROUTE_GRACE_POLLS
    )
    parser.add_argument(
        "--bake-minutes",
        type=float,
        default=60,
        help="the group's termination wait, for the `bake_end` output",
    )
    args = parser.parse_args(argv)
    if args.wrong_route_grace_polls < 1:
        parser.error("--wrong-route-grace-polls must be at least 1")
    return shift(
        deployment_id=args.deployment_id,
        cluster=args.cluster,
        service=args.service,
        arn=args.task_definition,
        deadline_seconds=args.deadline_seconds,
        interval_seconds=args.interval_seconds,
        alarms=args.alarm,
        wrong_route_grace_polls=args.wrong_route_grace_polls,
        bake_minutes=args.bake_minutes,
    )


if __name__ == "__main__":
    sys.exit(main())
