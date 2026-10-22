"""The AWS installer beta's install, read as text: the gates that need no run.

* D3: shellcheck at info level, with the check for set -e being suppressed, passes on the
  launcher and every library; shellcheck missing fails (never a skip).
* H1: the deploy role gains nothing. The two release scripts install calls are byte for
  byte main's, the role's generated policy is main's, and no file of the beta is one of
  scripts/iam_actions.py's sources.
* H2: every aws call the installer makes names a service, an operation and flags the CLI
  has (botocore's models), and every field its --query names is one the call prints; the
  one customization is on an exact list.
* H3: nothing bash 3.2 lacks and no GNU-only flag; every array expansion is guarded.
* A7 (static half): no file but lib/names.sh spells an infrastructure/cdk name.
* C4 (static half): the tools step runs npm with --ignore-scripts and pip with
  --require-hashes; pins/ agrees with infrastructure/cdk/requirements.txt's shared pins
  and with itself.
* H4: no account ID but the fixtures' in the beta's files and tests.
* U11 (static half): the installer's source is ASCII.

The runs are in backend/tests/installer_sweeps (Unit Tests) and the clean path in
test_installer_clean_path.py (here, so core-build has it too).
"""

from __future__ import annotations

import hashlib
import json
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from backend.tests.installer_sweeps.harness import queries
from backend.tests.installer_sweeps.harness.world import BASHES, bash_id

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[4]
INSTALLER = REPO_ROOT / "deploy" / "aws-installer"
LAUNCHER = INSTALLER / "controlvar-aws"
LIB = INSTALLER / "lib"
#: The files install's code lives in (lib/sweep.sh has its own static tests).
INSTALL_FILES = [
    LAUNCHER,
    *(
        LIB / f
        for f in (
            "names.sh",
            "env.sh",
            "messages.sh",
            "record.sh",
            "checks.sh",
            "steps.sh",
        )
    ),
]
BETA_FILES = [*INSTALL_FILES, LIB / "sweep.sh"]
PINS = INSTALLER / "pins"


def _code(path: Path) -> list[tuple[int, str]]:
    """The file's code lines, comments dropped, continuations joined."""
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


# --- D3: shellcheck -------------------------------------------------------------------


def test_shellcheck_passes_at_info_level():
    """Fails when shellcheck is missing, rather than skipping: no other gate reads the
    shell this way."""
    shellcheck = shutil.which("shellcheck")
    assert shellcheck, "shellcheck is not installed"
    done = subprocess.run(
        [
            shellcheck,
            "-S",
            "info",
            "-o",
            "check-set-e-suppressed",
            "-x",
            "controlvar-aws",
            *[f"lib/{p.name}" for p in BETA_FILES[1:]],
        ],
        cwd=INSTALLER,
        capture_output=True,
        text=True,
    )
    assert (done.returncode, done.stdout) == (0, ""), done.stdout + done.stderr


@pytest.mark.parametrize("bash", BASHES, ids=bash_id)
def test_every_file_parses_under_every_bash(bash):
    for path in BETA_FILES:
        done = subprocess.run([bash, "-n", str(path)], capture_output=True, text=True)
        assert done.returncode == 0, (path.name, done.stderr)


# --- H1: the deploy role gains nothing ----------------------------------------------------

#: main's bytes (v0.36.2): the scripts install runs unchanged, and the role's policy.
UNCHANGED = {
    "scripts/register_task_definition.sh": "d7afedab826f1fbc30f0048873bfbbf6de3a418c53e03072220186d2913f2f06",
    "scripts/run_migration_task.sh": "4a63d2aad24f38cade96a394635fb61b64be49144eef4bc22be394b188517f4c",
    "infrastructure/cdk/github-actions-deploy-policy.json": "0c556217785a56419aaa7530f202443b1a9a7ce79ae0090718bf482990180001",
}


@pytest.mark.parametrize("path", sorted(UNCHANGED))
def test_what_install_reuses_is_mains(path):
    """A change to these is a change to what the deploy role may do or to the steps the
    AWS guide's run proved; neither is the beta's to make."""
    assert (
        hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest() == UNCHANGED[path]
    )


def test_no_file_of_the_beta_is_a_source_of_the_deploy_roles_actions():
    sys.path.insert(0, str(REPO_ROOT / "scripts"))
    try:
        import iam_actions
    finally:
        sys.path.pop(0)
    sources = [p.resolve() for p in iam_actions.sources()]
    assert sources, "iam_actions.sources() found nothing"
    beta = [p for p in sources if INSTALLER in p.parents]
    assert beta == [], beta


# --- H2: every call is one the CLI has ------------------------------------------------------

#: The CLI's service names that botocore spells otherwise.
BOTOCORE = {"configservice": "config", "deploy": "codedeploy", "s3api": "s3"}
#: Options every operation takes.
GLOBAL_OPTIONS = {
    "query",
    "output",
    "region",
    "endpoint-url",
    "no-paginate",
    "max-items",
    "starting-token",
    "profile",
    "cli-input-json",
}
#: The CLI's own commands that are no botocore operation, exactly.
CUSTOMIZATIONS = {("ecr", "get-login-password")}
_CALL = re.compile(r"(?<![\w-])cv_(aws|read|write)\s+(.*)")


def _without_substitutions(text: str) -> str:
    """$( ... ) replaced by VALUE, nested ones included."""
    out, i = [], 0
    while i < len(text):
        if text.startswith("$(", i):
            depth, j = 1, i + 2
            while j < len(text) and depth:
                depth += {"(": 1, ")": -1}.get(text[j], 0)
                j += 1
            out.append("VALUE")
            i = j
        else:
            out.append(text[i])
            i += 1
    return "".join(out)


def _words(tail: str) -> list[str]:
    lexer = shlex.shlex(
        _without_substitutions(tail), posix=True, punctuation_chars=";&|()<>"
    )
    lexer.whitespace_split = True
    words = []
    for word in lexer:
        if word and set(word) <= set(";&|()<>"):
            break
        words.append(word)
    return words


def _calls() -> list[tuple[str, list[str]]]:
    """Every aws call install makes: (where, the CLI's arguments)."""
    found = []
    for path in INSTALL_FILES:
        for number, line in _code(path):
            for match in _CALL.finditer(line):
                kind, tail = match.groups()
                words = _words(tail)
                if kind in ("read", "write"):
                    words = words[2:]
                if not words or words[0].startswith("$") or words[0] == "{":
                    continue  # the wrappers themselves
                if words[0] == "--version":
                    continue  # the CLI's own version, which asks no service
                found.append((f"{path.relative_to(INSTALLER)}:{number}", words))
    return found


def _query(words: list[str]) -> str | None:
    if "--query" not in words:
        return None
    return re.sub(r"\$\{[^}]*\}|\$\w+", "x", words[words.index("--query") + 1])


#: How many aws calls install's text holds: a change is a call added or one the parser
#: stopped seeing, and either is checked here before it counts.
CALLS = 51


def test_the_call_list_is_complete():
    """The parser finds install's calls: every operation the fake answers in a clean
    install appears among them."""
    calls = _calls()
    assert len(calls) == CALLS, "\n".join(f"{w}: {' '.join(a)}" for w, a in calls)
    ops = {(w[0], w[1]) for _, w in calls}
    assert {
        ("sts", "get-caller-identity"),
        ("route53", "change-resource-record-sets"),
        ("ecr", "create-repository"),
        ("acm", "request-certificate"),
        ("ecr", "get-login-password"),
    } <= ops


def test_every_service_operation_and_flag_is_one_the_cli_has():
    """`aws` refuses an unknown flag (exit 252); a misspelt operation the same."""
    from botocore import xform_name
    from botocore.session import get_session

    session = get_session()
    problems, customized = [], set()
    for where, words in _calls():
        service, operation = words[0], words[1]
        model = session.get_service_model(BOTOCORE.get(service, service))
        operations = {xform_name(o).replace("_", "-"): o for o in model.operation_names}
        if operation not in operations:
            customized.add((service, operation))
            continue
        members = {
            xform_name(m).replace("_", "-")
            for m in model.operation_model(operations[operation]).input_shape.members
        }
        for flag in (w[2:] for w in words[2:] if w.startswith("--")):
            name = flag[3:] if flag.startswith("no-") and flag[3:] in members else flag
            if name not in members and flag not in GLOBAL_OPTIONS:
                problems.append(f"{where}: {service} {operation}: no flag --{flag}")
    assert problems == [], "\n".join(problems)
    assert customized == CUSTOMIZATIONS, customized


def test_every_query_field_is_one_the_call_prints():
    """A misspelt field reads as null, which a guard can take for "none"."""
    problems = []
    for where, words in _calls():
        query = _query(words)
        if query is None:
            continue
        for problem in queries.problems(words[0], words[1], words[2:], query):
            problems.append(f"{where}: {words[0]} {words[1]}: {problem}")
    assert problems == [], "\n".join(problems)


def test_no_read_can_be_cut_short():
    short = []
    for where, words in _calls():
        query = _query(words)
        if query is None:
            continue
        why = queries.cut_short(words[0], words[1], words[2:], query)
        if why:
            short.append(f"{where}: {words[0]} {words[1]}: {why}")
    assert short == [], "\n".join(short)


#: The only lines that start the CLI itself: the wrapper every call goes through, and the
#: probe (the same environment, with no profile).
WRAPPERS = {
    'env -i ${CV_ENV[@]+"${CV_ENV[@]}"} aws "$@"',
    'env -i ${kept[@]+"${kept[@]}"} aws sts get-caller-identity --endpoint-url http://127.0.0.1:9 > /dev/null 2> "$out" || rc=$?',
}
#: `aws` as a command: first in a command, or after env, exec, command, eval or xargs.
_AWS_COMMAND = re.compile(
    r"(^|[;&|(]\s*|\b(then|do|else|exec|command|eval|xargs|env|time|nohup)\s+(\S+\s+)*?)aws\s"
)


def _unquoted(line: str) -> str:
    """The line with what its quotes hold taken out: a message may name a command."""
    return re.sub(r"'[^']*'|\"(?:[^\"\\]|\\.)*\"", "''", line)


def test_every_aws_call_goes_through_the_wrapper():
    """An aws call outside env.sh's wrapper would run in the reader's own environment,
    with whatever keys, profile and endpoints the shell holds."""
    found = []
    for path in INSTALL_FILES:
        for number, line in _code(path):
            if " ".join(line.split()) in WRAPPERS:
                continue
            if _AWS_COMMAND.search(_unquoted(line)):
                found.append(f"{path.relative_to(INSTALLER)}:{number}: {line}")
    assert found == [], "\n".join(found)
    lines = {
        " ".join(line.split()) for path in INSTALL_FILES for _, line in _code(path)
    }
    assert WRAPPERS <= lines, WRAPPERS - lines


# --- H3: bash 3.2 and the BSD tools -----------------------------------------------------------

BANNED = {
    r"\$\{\w+,,\}": "${x,,}",
    r"\$\{\w+\^\^\}": "${x^^}",
    r"\$\{\w+@[QEPAa]\}": "${x@Q}",
    r"\bdeclare\s+-\w*A": "declare -A",
    r"\bdeclare\s+-\w*g": "declare -g",
    r"\blocal\s+-n\b": "local -n",
    r"\bmapfile\b": "mapfile",
    r"\breadarray\b": "readarray",
    r"&>>": "&>>",
    r"\|&": "|&",
    r"\bwait\s+-n\b": "wait -n",
    r"\bcoproc\b": "coproc",
    r";;&": ";;&",
    r"(?<!;);&(?!&)": ";&",
    r"\$\{\w+\[-\d+\]\}": "a negative array index",
    r"%\([^)]*\)T": 'printf "%(...)T"',
    r"\$\{?EPOCH(SECONDS|REALTIME)": "$EPOCHSECONDS",
    r"\bglobstar\b": "globstar",
    r"\bdate\s+(-\w+\s+)*-d\b": "date -d",
    r"\bsed\s+(-\w+\s+)*-i(\s|$)": "sed -i with no suffix",
    r"\bstat\s+-c\b": "stat -c",
    r"\bgrep\s+(-\w+\s+)*-\w*P": "grep -P",
    r"\bxargs\s+-\w*r": "xargs -r",
    r"(^|[\s;&|(])timeout\s": "timeout",
    r"\bbase64\s+-w\b": "base64 -w",
    r"\bsha1sum\b": "sha1sum",
    r"\bsort\s+(-\w+\s+)*-\w*V": "sort -V",
    r"\bfind\b.*-printf\b": "find -printf",
}


def test_nothing_bash_32_or_the_bsd_tools_lack():
    found = []
    for path in BETA_FILES:
        for number, line in _code(path):
            for pattern, name in BANNED.items():
                if re.search(pattern, line):
                    found.append(
                        f"{path.relative_to(INSTALLER)}:{number}: {name}: {line}"
                    )
    assert found == [], "\n".join(found)


def test_every_array_expansion_is_guarded():
    """Under set -u, bash 3.2 calls an empty array unbound: `${a[@]+"${a[@]}"}`."""
    guarded = re.compile(r'\$\{(\w+)\[([@*])\]\+"\$\{\1\[\2\]\}"\}')
    found = []
    for path in BETA_FILES:
        for number, line in _code(path):
            if re.search(r"\$\{\w+\[[@*]\]\}", guarded.sub("", line)):
                found.append(f"{path.relative_to(INSTALLER)}:{number}: {line}")
    assert found == [], "\n".join(found)


# --- A7 (static half), U11 (static half), H4 ------------------------------------------------------


def test_only_lib_names_sh_spells_an_infrastructure_name():
    """infrastructure/cdk's names live in lib/names.sh, which a CDK test holds to the
    real synth; anywhere else a renamed stack or family would go unseen."""
    found = [
        f"{path.relative_to(INSTALLER)}:{number}"
        for path in BETA_FILES
        if path.name != "names.sh"
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if "experimentation-" in line or "/staging/experimentation" in line
    ]
    assert found == [], found


def test_the_installers_source_is_ascii():
    for path in BETA_FILES:
        data = path.read_bytes()
        assert data.isascii(), (path.name, sorted({hex(b) for b in data if b > 0x7F}))
        assert b"\r" not in data and b"\x1b" not in data, path.name


#: The fixtures' account IDs, a typo of one, and AWS's own documentation examples;
#: anything else 12 digits long may be a real account's.
FIXTURE_ACCOUNTS = {
    "111111111111",
    "222222222222",
    "111111111112",
    "111122223333",
    "123456789012",
}


def test_no_account_id_but_the_fixtures():
    paths = [
        *sorted(INSTALLER.rglob("*")),
        *sorted((REPO_ROOT / "backend" / "tests" / "installer_sweeps").rglob("*")),
        *sorted(Path(__file__).parent.glob("test_installer*.py")),
    ]
    found = []
    for path in paths:
        if path.is_file() and path.suffix in (
            "",
            ".sh",
            ".py",
            ".json",
            ".in",
            ".lock",
        ):
            for number, line in enumerate(
                path.read_text(errors="replace").splitlines(), 1
            ):
                # A UUID's last part is 12 digits after a hyphen; an account ID never is.
                for match in re.finditer(
                    r"(?<![0-9A-Za-z-])\d{12}(?![0-9A-Za-z])", line
                ):
                    if match.group() not in FIXTURE_ACCOUNTS:
                        found.append(
                            f"{path.relative_to(REPO_ROOT)}:{number}: {match.group()}"
                        )
    assert found == [], found


# --- C4 (static half): the tools step ---------------------------------------------------------


def _tools_step() -> str:
    text = (LIB / "checks.sh").read_text()
    start = text.index("cv_tools_step() {")
    return text[start : text.index("\n}\n", start)]


def test_the_tools_step_runs_no_package_script_and_takes_only_hashed_packages():
    body = _tools_step()
    npm = [line for line in body.splitlines() if " npm " in line]
    pip = [line for line in body.splitlines() if " pip install " in line]
    assert len(npm) == 1 and "npm ci --ignore-scripts" in npm[0], npm
    assert (
        len(pip) == 1
        and "--require-hashes" in pip[0]
        and "--no-deps" in pip[0]
        and "pins/requirements.lock" in pip[0].replace('"$pins"', "pins")
    ), pip


def _pins(text: str) -> dict[str, str]:
    return dict(re.findall(r"^([A-Za-z0-9_.-]+)==([0-9][^\s;\\]*)", text, re.M))


def test_the_hashed_lock_agrees_with_the_cdk_apps_pins():
    """Fails whenever infrastructure/cdk/requirements.txt moves a pin it shares with
    pins/ and `make installer-pins` was not run."""
    cdk = _pins((REPO_ROOT / "infrastructure" / "cdk" / "requirements.txt").read_text())
    lock_text = (PINS / "requirements.lock").read_text()
    lock = _pins(lock_text)
    shared = sorted(set(cdk) & set(lock))
    assert shared == ["aws-cdk-lib", "constructs"], shared
    assert {name: lock[name] for name in shared} == {name: cdk[name] for name in shared}
    wanted = "".join(
        line + "\n"
        for line in (REPO_ROOT / "infrastructure" / "cdk" / "requirements.txt")
        .read_text()
        .splitlines()
        if re.match(r"^(aws-cdk-lib|constructs)==", line)
    )
    assert (PINS / "requirements.in").read_text() == wanted
    assert "make installer-pins" in lock_text.splitlines()[1]
    entries = re.split(r"\n(?=[A-Za-z0-9_.-]+==)", lock_text.split("\n", 2)[2])
    unhashed = [
        e.split("==")[0] for e in entries if e.strip() and "--hash=sha256:" not in e
    ]
    assert unhashed == [], unhashed


def test_the_cdk_cli_is_pinned_in_both_files():
    package = json.loads((PINS / "package.json").read_text())
    lock = json.loads((PINS / "package-lock.json").read_text())
    assert package["dependencies"] == {"aws-cdk": "2.1144.0"}
    assert lock["packages"][""]["dependencies"] == {"aws-cdk": "2.1144.0"}
    assert lock["packages"]["node_modules/aws-cdk"]["version"] == "2.1144.0"
    assert lock["packages"]["node_modules/aws-cdk"]["integrity"].startswith("sha512-")
