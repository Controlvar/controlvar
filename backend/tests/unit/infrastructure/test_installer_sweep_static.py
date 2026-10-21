"""The AWS installer beta's sweep library, read as text (G1's and G3's static halves).

deploy/aws-installer/lib/sweep.sh records an account before install's first write and
after uninstall. Read without running it:

* G1: it only reads. Every call it makes is a list, describe or get operation,
  through cv_aws (lib/env.sh's wrapper), never a bare ``aws``; its 58 regional
  types name their region, its 10 global ones do not; the fake models every one.
* G3: the kept list is exact names. No value in lib/names.sh holds a pattern
  character, the service-linked roles are five whole lines, and the only patterns
  the kept list matches with are the pinned ones (the file dispatch, AWS's own
  ``alias/aws/`` names and KMS keys, and character classes for an ID and a
  revision number).
* The names stay in lib/names.sh: lib/sweep.sh spells no infrastructure/cdk name.

The libraries run in backend/tests/installer_sweeps (Unit Tests, not core-build).
"""

from __future__ import annotations

import re
import shlex
import subprocess
from pathlib import Path

import pytest

from backend.tests.installer_sweeps.harness import fake_aws
from backend.tests.installer_sweeps.harness.world import BASHES, bash_id

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
LIB = REPO_ROOT / "deploy" / "aws-installer" / "lib"
SWEEP = LIB / "sweep.sh"
NAMES = LIB / "names.sh"
READ_VERBS = ("list-", "describe-", "get-")


def _code(path: Path) -> list[tuple[int, str]]:
    """The file's lines that are code, comments dropped, continuations joined."""
    out, pending, start = [], "", 0
    for number, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.split(" #", 1)[0] if not raw.lstrip().startswith("#") else ""
        if not pending:
            start = number
        if line.rstrip().endswith("\\"):
            pending += line.rstrip()[:-1] + " "
            continue
        joined = (pending + line).strip()
        pending = ""
        if joined:
            out.append((start, joined))
    return out


def _reads() -> list[tuple[int, str, list[str]]]:
    """Every read: (line, the kind, the aws arguments), a per-item type twice."""
    found = []
    for number, line in _code(SWEEP):
        words = line.split(None, 1)
        if not words or words[0] not in ("_sweep_q", "_sweep_each"):
            continue
        argv = shlex.split(line)
        if argv[0] == "_sweep_q":
            found.append((number, argv[1] + "/" + argv[2], argv[3:]))
        else:
            split = argv.index("--")
            found.append((number, argv[1] + "/" + argv[2], argv[3:split]))
            found.append(
                (number, argv[1] + "/" + argv[2] + " (each)", argv[split + 1 :])
            )
    return found


def test_every_read_is_a_list_describe_or_get_operation():
    reads = _reads()
    assert len(reads) == 58 * 1 + 3 + 10, len(
        reads
    )  # 58 regional and 10 global, 3 with items
    for number, kind, argv in reads:
        service, operation = argv[0], argv[1]
        assert operation.startswith(READ_VERBS), (
            f"lib/sweep.sh:{number}: {service} {operation}"
        )


#: ``aws`` or ``cv_aws`` where a command starts: a line's start, after ; & | ( or
#: $( , or after if, then, do, ! and the like.
_COMMAND = re.compile(
    r"(?:^|[;&|(]|\$\(|\b(?:if|then|do|else|while|until)\b|!)\s*(cv_aws|aws)\b"
)


def test_every_aws_call_goes_through_cv_aws_and_reads():
    calls = [(number, line) for number, line in _code(SWEEP) if _COMMAND.search(line)]
    allowed = {
        'cv_aws "$@" --output json > "$out" 2> "$err" || rc=$?',
        "if ! got=$(cv_aws sts get-caller-identity --query Account --output text); then",
    }
    assert {line for _, line in calls} == allowed, calls


def test_regional_reads_name_their_region_and_global_ones_do_not():
    for number, kind, argv in _reads():
        region = argv[argv.index("--region") + 1] if "--region" in argv else None
        if kind.startswith("global/"):
            assert region is None, f"lib/sweep.sh:{number}"
        else:
            assert region == "$r", f"lib/sweep.sh:{number}: {argv}"


def test_the_fake_models_every_read():
    modelled = set(fake_aws.modelled_operations())
    missing = sorted({(a[0], a[1]) for _, _, a in _reads()} - modelled)
    assert missing == [], f"reads the fake does not answer: {missing}"


def _names() -> dict[str, list[str]]:
    """Every NAMES_ variable lib/names.sh defines, read by bash itself."""
    script = (
        f'. "{NAMES}"; for v in $(compgen -v NAMES_); do '
        'eval "set -- \\"\\${$v[@]}\\""; for x in "$@"; do printf "%s=%s\\n" "$v" "$x"; done; done'
    )
    done = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=True
    )
    out: dict[str, list[str]] = {}
    for line in done.stdout.splitlines():
        name, value = line.split("=", 1)
        out.setdefault(name, []).append(value)
    return out


def test_names_are_exact_values():
    names = _names()
    assert sorted(names) == [
        "NAMES_ALARM_TOPIC",
        "NAMES_DB_KEY_DESCRIPTION",
        "NAMES_FAMILIES",
        "NAMES_TABLES",
    ]
    assert [len(names[n]) for n in sorted(names)] == [1, 1, 3, 3]
    for name, values in names.items():
        for value in values:
            assert value and not set(value) & set("*?[]"), (name, value)


def _kept_function() -> str:
    text = SWEEP.read_text()
    start = text.index("_sweep_kept() {")
    return text[start : text.index("\n}\n", start)]


def test_the_kept_list_matches_with_the_pinned_patterns_only():
    """A pattern in the kept list is a hidden wildcard: `AWSServiceRoleFor*` would
    keep a stray role (U10). The only patterns are pinned here."""
    patterns = [
        line.strip()
        for line in _kept_function().splitlines()
        if re.match(r"^\s*[^\s#()][^()]*\)(\s|$)", line) and "case " not in line
    ]
    assert patterns == [
        "*/kms-aliases.txt)",
        "alias/aws/*) return 0 ;;",
        "*/kms-keys.txt)",
        '"AWS${tab}"*) return 0 ;;',
        "*[!0-9a-f-]*) return 1 ;;",
        "global/iam-roles.txt)",
        "us-west-2/dynamodb-backups.txt)",
        "us-west-2/ecs-td-delete-in-progress.txt)",
        "*[!0-9]*) ;;",
        "*) return 0 ;;",
        "us-west-2/ecs-td-families-inactive.txt)",
        "us-west-2/sns-subscriptions.txt)",
    ], patterns
    assert "[[" not in SWEEP.read_text()  # `[[ x == y* ]]` would be a pattern too


def test_the_service_linked_roles_are_five_whole_lines():
    text = SWEEP.read_text()
    block = text[
        text.index("_SWEEP_KEPT_ROLES=(") : text.index(
            ")\n", text.index("_SWEEP_KEPT_ROLES=(")
        )
    ]
    entries = re.findall(r'"([^"]+)"', block)
    assert entries == [
        "/aws-service-role/ecs.amazonaws.com/ AWSServiceRoleForECS",
        "/aws-service-role/elasticloadbalancing.amazonaws.com/ AWSServiceRoleForElasticLoadBalancing",
        "/aws-service-role/rds.amazonaws.com/ AWSServiceRoleForRDS",
        "/aws-service-role/elasticache.amazonaws.com/ AWSServiceRoleForElastiCache",
        "/aws-service-role/ecs.application-autoscaling.amazonaws.com/ "
        "AWSServiceRoleForApplicationAutoScaling_ECSService",
    ]


def test_the_sweep_library_spells_no_infrastructure_name():
    """infrastructure/cdk's names live in lib/names.sh only (PE 18)."""
    assert "experimentation-" not in SWEEP.read_text()


@pytest.mark.parametrize("bash", BASHES, ids=bash_id)
def test_the_libraries_parse_under_every_bash(bash):
    for path in (SWEEP, NAMES):
        done = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
        assert done.returncode == 0, (path.name, done.stderr)
