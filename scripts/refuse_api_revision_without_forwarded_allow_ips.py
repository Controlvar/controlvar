#!/usr/bin/env python3
"""Refuse to deploy onto an API task definition without FORWARDED_ALLOW_IPS.

    python3 -I -S scripts/refuse_api_revision_without_forwarded_allow_ips.py \\
        (--family <the API's task definition family> | --task-definition <ARN>) \\
        --release <the release checkout> \\
        --environment <env> --fargate-stack <stack name>

The API image builds the list of proxies whose X-Forwarded-For it believes
when it starts: loopback, the networks its container is attached to, and the
entries of FORWARDED_ALLOW_IPS (backend/app/core/proxy_trust.py). On AWS the
load balancer is not on the API tasks' own network, so the API task definition
sets FORWARDED_ALLOW_IPS to the VPC's range
(infrastructure/cdk/stacks/fargate_service_stack.py). Deploy registers the API
revision from the family's newest ACTIVE revision
(scripts/register_task_definition.sh), and only `cdk deploy` changes that
revision's environment. A Fargate stack last deployed from an older checkout
therefore gives such an image no FORWARDED_ALLOW_IPS, and the API would see
every request as coming from the load balancer: its access lines and its rate
limits would carry the load balancer's address instead of the user's.

Deploy runs this twice, with no `if:`:

* `--family`, before anything is built or changed: the family's newest ACTIVE
  revision, which the registration is about to copy;
* `--task-definition`, after the API revision is registered and before the
  CodeDeploy deployment is created: what ECS STORED for that ARN, not the
  JSON that was sent.

It passes when that revision has exactly one container named `backend`, whose
`environment` names FORWARDED_ALLOW_IPS exactly once (and whose `secrets` do
not name it), with at least one entry, and every entry accepted under the
staging and production rules: `entry_problems` and `every_address_problems`
of backend.app.core.proxy_trust, imported from this checkout and not repeated
here. Anything else is refused.

A release from before that list was built at start-up passes a fixed default
list to uvicorn in its backend/docker-entrypoint.sh, and needs nothing from the
task definition. Such a release is not checked, with a notice, only when it is
positively identified: its entrypoint has a command line (not a comment)
containing OLD_DEFAULT. A different entrypoint, none at all, or one that
cannot be read is checked.

Deploy has no setup-python, so the runner's own python3 runs this, as
`python3 -I -S`: the standard library and this repository, no site-packages.

READ-ONLY: `OPERATIONS` is the whole allow-list, and its one call reads.

Exit status: 0 go on (the setting is usable, or the release predates it); 1
refused; 2 could not tell (bad arguments, the AWS call failed, its answer
could not be read, or it describes another revision). 2 also stops the
deploy: "I could not read it" is not "it is set".
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent
REPO_ROOT = SCRIPTS.parent
# `python3 -I -S` puts neither directory on sys.path.
sys.path.insert(0, str(REPO_ROOT))
sys.path.insert(0, str(SCRIPTS))
from public_text import redact, short_arn

from backend.app.core import proxy_trust

#: Every AWS CLI operation this script may run. It reads.
OPERATIONS = frozenset({("ecs", "describe-task-definition")})

OK, REFUSED, UNKNOWN = 0, 1, 2

CONTAINER = "backend"
VARIABLE = proxy_trust.SETTING

#: What a release from before the list was built at start-up has on its
#: uvicorn command line in backend/docker-entrypoint.sh (measured on every
#: release tag from v0.2.3 to v0.39.0).
OLD_DEFAULT = (
    "${FORWARDED_ALLOW_IPS:-127.0.0.1,::1,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16}"
)
ENTRYPOINT = Path("backend") / "docker-entrypoint.sh"
#: Read at most this much of the entrypoint; a longer one is not identified.
MAX_ENTRYPOINT_BYTES = 1024 * 1024

GUIDE = "docs/deployment/deployment-guide.md#16-the-stacks"

#: What has already happened when each check runs, and what has not.
DONE = {
    "family": "Nothing has been built or changed.",
    "task-definition": (
        "The snapshot was taken and the migration was applied; no CodeDeploy "
        "deployment was created, so the revision serving before this run still "
        "serves."
    ),
}


class AwsError(Exception):
    """An AWS CLI call failed; the message is its stderr."""


def run_aws(argv: Sequence[str]) -> dict:
    if tuple(argv[:2]) not in OPERATIONS:
        raise ValueError(f"not a read-only operation this script may run: {argv[:2]}")
    proc = subprocess.run(
        ["aws", *argv, "--output", "json"],
        capture_output=True,
        text=True,
        check=False,
        stdin=subprocess.DEVNULL,
    )
    if proc.returncode != 0:
        raise AwsError(proc.stderr.strip() or f"aws exited {proc.returncode}")
    return json.loads(proc.stdout or "{}")


def _one_line(value: object, limit: int = 200) -> str:
    """Printed inside a workflow command, which is one line."""
    return redact(" ".join(str(value)[:limit].split()))


def predates_the_list(release: Path) -> bool:
    """True only when *release*'s entrypoint has a command line containing
    OLD_DEFAULT. Missing, unreadable, longer than MAX_ENTRYPOINT_BYTES, or
    anything else: False, and the revision is checked."""
    try:
        with open(release / ENTRYPOINT, "rb") as handle:
            data = handle.read(MAX_ENTRYPOINT_BYTES + 1)
    except OSError:
        return False
    if len(data) > MAX_ENTRYPOINT_BYTES:
        return False
    marker = OLD_DEFAULT.encode("ascii")
    return any(
        marker in line and not line.lstrip().startswith(b"#")
        for line in data.splitlines()
    )


def read_revision(
    aws: Callable[[Sequence[str]], dict], *, arn: str = "", family: str = ""
) -> dict:
    """What ECS answers for the ARN, or for the family's newest ACTIVE revision."""
    answer = aws(
        ["ecs", "describe-task-definition", "--task-definition", arn or family]
    )
    stored = answer.get("taskDefinition") if isinstance(answer, dict) else None
    if not isinstance(stored, dict):
        raise ValueError("describe-task-definition returned no taskDefinition")
    read = short_arn(stored.get("taskDefinitionArn") or "no ARN")
    if arn and stored.get("taskDefinitionArn") != arn:
        raise ValueError(f"asked for {short_arn(arn)}, read {read}")
    if family and (stored.get("family") != family or stored.get("status") != "ACTIVE"):
        raise ValueError(
            f"asked for the newest ACTIVE revision of {family}, read {read} "
            f"(family {stored.get('family')}, {stored.get('status')})"
        )
    return stored


def why_unusable(stored: dict) -> str | None:
    """Why this revision does not give the API a usable FORWARDED_ALLOW_IPS,
    or None."""
    containers = [
        c
        for c in stored.get("containerDefinitions") or []
        if isinstance(c, dict) and c.get("name") == CONTAINER
    ]
    if len(containers) != 1:
        return f"it has {len(containers)} containers named `{CONTAINER}`, not one"
    (container,) = containers
    if any(
        isinstance(entry, dict) and entry.get("name") == VARIABLE
        for entry in container.get("secrets") or []
    ):
        return (
            f"its `{CONTAINER}` container takes {VARIABLE} from a secret, which "
            "this check cannot read"
        )
    values = [
        entry.get("value")
        for entry in container.get("environment") or []
        if isinstance(entry, dict) and entry.get("name") == VARIABLE
    ]
    if not values:
        return f"its `{CONTAINER}` container does not set {VARIABLE}"
    if len(values) > 1:
        return f"its `{CONTAINER}` container sets {VARIABLE} {len(values)} times"
    value = values[0] if isinstance(values[0], str) else ""
    shown = f"{VARIABLE}={_one_line(json.dumps(value), 80)}"
    problems = proxy_trust.entry_problems(value) + proxy_trust.every_address_problems(
        value
    )
    if problems:
        return (
            f"its `{CONTAINER}` container sets {shown}, which the API refuses at "
            f"start-up: {' '.join(problems)}"
        )
    if not proxy_trust.entries(value):
        return (
            f"its `{CONTAINER}` container sets {shown}, which names no address "
            "or network"
        )
    return None


def main(
    argv: Sequence[str] | None = None,
    aws: Callable[[Sequence[str]], dict] = run_aws,
) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    which = parser.add_mutually_exclusive_group(required=True)
    which.add_argument("--family")
    which.add_argument("--task-definition")
    parser.add_argument("--release", required=True, type=Path)
    parser.add_argument("--environment", required=True)
    parser.add_argument("--fargate-stack", required=True)
    try:
        args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    except SystemExit:
        return UNKNOWN
    mode = "family" if args.family is not None else "task-definition"
    asked = (args.family if mode == "family" else args.task_definition).strip()
    if not asked:
        print(f"::error title=No API task definition to check::--{mode} is empty.")
        return UNKNOWN

    if predates_the_list(args.release):
        print(
            f"::notice title={VARIABLE} not checked::This release's {ENTRYPOINT} "
            "passes uvicorn a fixed list of trusted proxies, so its API does not "
            f"read {VARIABLE} from the task definition."
        )
        return OK

    try:
        if mode == "family":
            stored = read_revision(aws, family=asked)
        else:
            stored = read_revision(aws, arn=asked)
    except (AwsError, ValueError, json.JSONDecodeError) as exc:
        print(
            "::error title=Could not read the API task definition::"
            f"{_one_line(exc)}. {DONE[mode]} This deploy stops here: a check "
            f"that could not read the task definition did not find {VARIABLE} "
            "set."
        )
        return UNKNOWN

    name = short_arn(stored.get("taskDefinitionArn") or asked)
    why = why_unusable(stored)
    if why is None:
        print(f"{name}: {VARIABLE} is set on `{CONTAINER}` (as stored by ECS)")
        return OK

    where = (
        "is the newest revision of its family, which this run would register "
        "this release's image from,"
        if mode == "family"
        else "was registered from the newest revision of its family,"
    )
    print(
        f"::error title=This API task definition does not set a usable {VARIABLE}::"
        f"{name} {where} and {_one_line(why, 1200)}. This release's API believes "
        "X-Forwarded-For only from loopback, its container's own networks and "
        f"{VARIABLE}, and the load balancer is on none of its networks: without "
        "the setting, every request would carry the load balancer's address. "
        f"Fix: run `cdk deploy {args.fargate_stack}` for {args.environment} "
        f"from this release's checkout, pinned to what is live ({GUIDE}), which "
        f"sets {VARIABLE} on the API task; then run Deploy again. {DONE[mode]}"
    )
    return REFUSED


if __name__ == "__main__":
    sys.exit(main())
