"""Which workflows run with a privileged trigger, and what each may contain.

A workflow triggered by ``pull_request_target`` or ``workflow_run`` runs the
default branch's definition with the base repository's token, for an event a
pull request from a fork can start. Which workflows do that, and what they do
with it, is pinned here rather than reviewed once:

* discovery: every file in ``.github/workflows`` but ``issue-triage.yml.example``
  is read, through ``_workflow_graph.workflow_files()``, and there are exactly
  WORKFLOW_COUNT of them;
* the triggers: each file is loaded with the duplicate-key-refusing loader,
  the trigger key is read in both forms (a bare ``on``, which that loader
  reads as True, and a quoted ``"on"``), a workflow carrying both forms is
  refused, every workflow has at least one trigger, and no top-level key is
  one GitHub does not define;
* every trigger name is ORDINARY or PRIVILEGED; any other is unclassified and
  fails until it is classified here;
* the workflows that use a PRIVILEGED trigger are exactly
  PRIVILEGED_WORKFLOWS;
* a privileged workflow uses only the job and step keys GitHub defines, in
  their own case; it does not check out a ref or another repository (input
  names compared in any case), name a pull request's head (or ``refs/pull``,
  ``git fetch ... pull/``, ``gh pr checkout``) in a ``with:`` or ``run:``,
  run ``git`` at all, or ``gh pr checkout`` or ``gh repo clone``, in a
  ``run:``, download an artifact, read any secret but ``GITHUB_TOKEN``
  (``toJSON(secrets)`` and ``secrets: inherit`` included), put an expression
  in a ``run:`` line or a github-script ``script:`` (values arrive through
  ``env:``), use another repository's action or workflow without pinning it
  to a 40-character commit SHA, ask for ``write-all`` or ``id-token: write``,
  or name an ``environment:``. There is no exemption for any file. The head
  match is case-insensitive, so in shell it also catches ``--jq .head.sha``
  on purpose, and a variable such as ``$HEAD_SHA``: rename it;
* a reusable workflow or local action a privileged workflow calls is held to
  the same rules; one that cannot be read here (another repository's
  workflow, a JavaScript or Docker action) is refused;
* no job of a ``pull_request`` workflow, or of a workflow it calls, names an
  ``environment:``.

Every rule is also planted against the real privileged workflow below, so a
rule that stops firing fails here. The files are read from the filesystem,
never through git, so the same holds in core_build.sh's copy, which has no
``.git`` and no ``modules/``.
"""

from __future__ import annotations

import copy
import os
import re
from pathlib import Path
from typing import Any, Callable, Dict, FrozenSet, Iterator, List, Optional, Set

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = pytest.mark.unit

if not wg.WORKFLOWS.is_dir():
    pytest.skip("this tree has no .github/workflows", allow_module_level=True)

#: In the directory, but not a workflow.
NOT_WORKFLOWS = frozenset({"issue-triage.yml.example"})

#: Every workflow, counted. Adding or removing one changes this, on purpose.
WORKFLOW_COUNT = 38

#: Triggers that run a workflow for the repository's own pushes, schedules,
#: dispatches and calls, or for a pull request with the token GitHub gives
#: that pull request.
ORDINARY = frozenset(
    {"push", "pull_request", "workflow_dispatch", "workflow_call", "schedule"}
)

#: Triggers that run the default branch's definition with the base
#: repository's token, for an event a fork's pull request can start.
PRIVILEGED = frozenset({"pull_request_target", "workflow_run"})

#: The workflows that use a PRIVILEGED trigger, exactly.
PRIVILEGED_WORKFLOWS = frozenset({"dependabot-automerge.yml"})

#: The top-level keys GitHub defines for a workflow, besides the bare `on`
#: (True to the loader).
TOP_LEVEL_KEYS = frozenset(
    {
        "name",
        "run-name",
        "on",
        "permissions",
        "env",
        "defaults",
        "concurrency",
        "jobs",
    }
)

#: The keys GitHub defines for a job, and for a step. In a privileged workflow
#: any other key, or one of these in another case, fails.
JOB_KEYS = frozenset(
    {
        "name",
        "permissions",
        "needs",
        "if",
        "runs-on",
        "environment",
        "concurrency",
        "outputs",
        "env",
        "defaults",
        "steps",
        "timeout-minutes",
        "strategy",
        "continue-on-error",
        "container",
        "services",
        "uses",
        "with",
        "secrets",
    }
)
STEP_KEYS = frozenset(
    {
        "id",
        "if",
        "name",
        "uses",
        "run",
        "working-directory",
        "shell",
        "with",
        "env",
        "continue-on-error",
        "timeout-minutes",
    }
)

# The rules, as they are named in a failure.
KEY = "has a key GitHub does not define"
CHECKOUT = "checks out a ref or another repository"
HEAD = "names a pull request head"
#: Added to HEAD in code: what trips it there most often, and the way out.
HEAD_IN_CODE = (
    " (a shell variable whose name begins with head, such as $HEAD_SHA, "
    "matches too: rename the variable)"
)
GIT = "runs git, or checks out code with gh"
DOWNLOAD = "downloads an artifact"
SECRET = "reads a secret other than GITHUB_TOKEN"
EXPRESSION = "has an expression in code (pass it through env:)"
UNPINNED = "uses something not pinned to a 40-character SHA"
WRITE_ALL = "asks for write-all"
ID_TOKEN = "asks for id-token: write"
ENVIRONMENT = "names an environment"
FOREIGN_WORKFLOW = "calls a workflow from another repository"
NOT_COMPOSITE = "calls a local action that is not composite"
UNREADABLE = "calls something that cannot be read"

_PINNED = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+@[0-9a-f]{40}")
_SECRETS = re.compile(r"\bsecrets\b", re.I)
#: What may follow `secrets`: the token, and nothing else.
_TOKEN_ONLY = re.compile(
    r"""\s*(?:\.\s*GITHUB_TOKEN|\[\s*['"]GITHUB_TOKEN['"]\s*\])(?![\w-])""", re.I
)
#: A pull request's head, by any of the names a payload or a command gives it:
#: `head.sha`, `head.ref`, `head.repo`, `head_ref`, `workflow_run.head_sha`,
#: `head_branch`, `head_repository`, `head_commit`; a pull ref; a fetch of
#: one; the CLI's checkout. Case-insensitive, as GitHub's expressions are; in
#: shell text that also catches `--jq .head.sha` on purpose, and with it a
#: variable such as $HEAD_SHA, which is renamed rather than excused.
_HEAD = re.compile(
    r"refs/pull|\bhead[._]|\bgit\s+fetch\b[^\n]*\bpull/|\bgh\s+pr\s+checkout\b",
    re.I,
)
#: git in any form, and the gh commands that check code out. A head SHA can
#: arrive through `env:`, where _HEAD cannot see it; no privileged workflow
#: here runs git, so none may.
_GIT = re.compile(r"\bgit\b|\bgh\s+(?:pr\s+checkout|repo\s+clone)\b", re.I)
_EXPRESSION = re.compile(r"\$\{\{")
_RUN_DOWNLOAD = re.compile(r"\bgh\s+run\s+download\b", re.I)


# --------------------------------------------------------------------------
# Reading
# --------------------------------------------------------------------------


def _read(path: Path) -> Dict[str, Any]:
    """A YAML file through the repository's loader; one it refuses fails,
    named."""
    try:
        document = wg.load(path)
    except Exception as exc:  # the loader's AssertionError, or a YAMLError
        raise AssertionError(f"{path.name}: cannot be read: {exc}") from exc
    assert isinstance(document, dict), f"{path.name}: not a mapping"
    return document


def _triggers(name: str, workflow: Dict[str, Any]) -> Set[Any]:
    try:
        on = wg.triggers(workflow)
    except (ValueError, TypeError) as exc:
        raise AssertionError(f"{name}: {exc}") from exc
    assert isinstance(on, dict) and on, f"{name}: no trigger"
    return set(on)


def _top_level_problems(name: str, workflow: Dict[str, Any]) -> List[str]:
    return [
        f"{name}: top-level key {key!r} is not one GitHub defines"
        for key in workflow
        if not (key is True or (isinstance(key, str) and key in TOP_LEVEL_KEYS))
    ]


@pytest.fixture(scope="module")
def workflows() -> Dict[str, Dict[str, Any]]:
    return {path.name: _read(path) for path in wg.workflow_files()}


@pytest.fixture(scope="module")
def triggers(workflows: Dict[str, Dict[str, Any]]) -> Dict[str, Set[Any]]:
    return {name: _triggers(name, w) for name, w in workflows.items()}


# --------------------------------------------------------------------------
# What a privileged workflow may not contain
# --------------------------------------------------------------------------


def _strings(value: Any) -> Iterator[str]:
    """Every key and every scalar under `value`, as text."""
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)
    elif value is not None:
        yield str(value)


def _document_problems(where: str, document: Dict[str, Any]) -> List[str]:
    """The rules that hold for every text in a workflow or an action."""
    problems = []
    for text in _strings(document):
        for match in _SECRETS.finditer(text):
            if not _TOKEN_ONLY.match(text, match.end()):
                problems.append(f"{where}: {SECRET}: {text.strip()[:80]!r}")
        if text.strip().lower() == "write-all":
            problems.append(f"{where}: {WRITE_ALL}")
    return problems


def _permission_problems(where: str, permissions: Any) -> List[str]:
    if isinstance(permissions, dict) and any(
        str(key).lower() == "id-token" and str(value).lower() == "write"
        for key, value in permissions.items()
    ):
        return [f"{where}: {ID_TOKEN}"]
    return []


def _key_problems(
    where: str, mapping: Dict[Any, Any], allowed: FrozenSet[str]
) -> List[str]:
    return [
        f"{where}: {KEY}: {key!r}"
        for key in mapping
        if not (isinstance(key, str) and key in allowed)
    ]


def _inputs(with_: Any) -> Dict[str, List[Any]]:
    """An action's `with:` by lower-cased input name. An action reads an input
    from the upper-cased ``INPUT_<NAME>``, so `Ref` and `ref` are one input."""
    inputs: Dict[str, List[Any]] = {}
    for key, value in (with_ if isinstance(with_, dict) else {}).items():
        inputs.setdefault(str(key).lower(), []).append(value)
    return inputs


def _step_problems(where: str, step: Any, seen: Set[Path]) -> List[str]:
    if not isinstance(step, dict):
        return [f"{where}: is not a mapping"]
    problems = _key_problems(where, step, STEP_KEYS)
    inputs = _inputs(step.get("with"))
    code = [str(step["run"])] if step.get("run") is not None else []
    uses = step.get("uses")
    if uses is not None:
        uses = str(uses)
        action = uses.split("@", 1)[0].lower()
        if uses.startswith("./"):
            problems += _local_action_problems(where, uses, seen)
        elif not _PINNED.fullmatch(uses):
            problems.append(f"{where}: {UNPINNED}: {uses}")
        if action == "actions/checkout" and ("ref" in inputs or "repository" in inputs):
            problems.append(f"{where}: {CHECKOUT}")
        if "download-artifact" in action:
            problems.append(f"{where}: {DOWNLOAD}: {uses}")
        if action == "actions/github-script":
            code += [str(script) for script in inputs.get("script", [])]
    if any(_HEAD.search(text) for text in _strings(step.get("with"))):
        problems.append(f"{where}: {HEAD} in with:")
    for text in code:
        if _HEAD.search(text):
            problems.append(f"{where}: {HEAD} in code{HEAD_IN_CODE}")
        if _GIT.search(text):
            problems.append(f"{where}: {GIT}")
        if _EXPRESSION.search(text):
            problems.append(f"{where}: {EXPRESSION}")
        if _RUN_DOWNLOAD.search(text):
            problems.append(f"{where}: {DOWNLOAD}: gh run download")
    return problems


def _local_action_problems(where: str, uses: str, seen: Set[Path]) -> List[str]:
    root = wg.REPO_ROOT.resolve()
    directory = (root / uses).resolve()
    files = [
        directory / name
        for name in ("action.yml", "action.yaml")
        if (directory / name).is_file()
    ]
    if root not in directory.parents or len(files) != 1:
        return [f"{where}: {UNREADABLE}: {uses}"]
    if files[0] in seen:
        return []
    seen.add(files[0])
    action = _read(files[0])
    label = f"{where} -> {files[0].relative_to(root).as_posix()}"
    runs = action.get("runs") or {}
    if runs.get("using") != "composite":
        return [f"{where}: {NOT_COMPOSITE}: {uses} ({runs.get('using')!r})"]
    problems = _document_problems(label, action)
    for index, step in enumerate(runs.get("steps") or [], start=1):
        problems += _step_problems(f"{label} step {index}", step, seen)
    return problems


def _called_workflow_problems(where: str, uses: str, seen: Set[Path]) -> List[str]:
    if not uses.startswith("./"):
        return [f"{where}: {FOREIGN_WORKFLOW}: {uses}"]
    path = (wg.REPO_ROOT.resolve() / uses).resolve()
    if path.parent != wg.WORKFLOWS.resolve() or not path.is_file():
        return [f"{where}: {UNREADABLE}: {uses}"]
    return [
        f"{where} -> {problem}"
        for problem in privileged_problems(path.name, _read(path), seen)
    ]


def privileged_problems(
    name: str, workflow: Dict[str, Any], seen: Optional[Set[Path]] = None
) -> List[str]:
    """Every way `workflow` (named `name`), and what it calls, breaks the
    rules for a privileged workflow. Empty when it keeps them."""
    seen = set() if seen is None else seen
    path = (wg.WORKFLOWS / name).resolve()
    if path in seen:
        return []
    seen.add(path)
    problems = _document_problems(name, workflow)
    problems += _permission_problems(name, workflow.get("permissions"))
    for job_id, job in (workflow.get("jobs") or {}).items():
        where = f"{name}/{job_id}"
        if not isinstance(job, dict):
            problems.append(f"{where}: is not a mapping")
            continue
        problems += _key_problems(where, job, JOB_KEYS)
        problems += _permission_problems(where, job.get("permissions"))
        if "environment" in job:
            problems.append(f"{where}: {ENVIRONMENT}")
        if any(_HEAD.search(text) for text in _strings(job.get("with"))):
            problems.append(f"{where}: {HEAD} in with:")
        if job.get("uses") is not None:
            uses = str(job["uses"])
            if not uses.startswith("./") and not _PINNED.fullmatch(uses):
                problems.append(f"{where}: {UNPINNED}: {uses}")
            problems += _called_workflow_problems(where, uses, seen)
        for index, step in enumerate(job.get("steps") or [], start=1):
            problems += _step_problems(f"{where} step {index}", step, seen)
    return problems


def _environment_problems(
    name: str, workflow: Dict[str, Any], seen: Optional[Set[Path]] = None
) -> List[str]:
    """A job, or a job of a workflow it calls, that names an environment."""
    seen = set() if seen is None else seen
    problems = []
    for job_id, job in (workflow.get("jobs") or {}).items():
        if "environment" in job:
            problems.append(f"environment in a pull_request job: {name}/{job_id}")
        uses = job.get("uses")
        if isinstance(uses, str) and uses.startswith("./"):
            path = (wg.REPO_ROOT / uses).resolve()
            if path not in seen:
                seen.add(path)
                problems += _environment_problems(
                    f"{name}/{job_id} -> {path.name}", _read(path), seen
                )
    return problems


# --------------------------------------------------------------------------
# The tree
# --------------------------------------------------------------------------


def test_every_file_in_the_directory_is_read_and_counted():
    listing = {entry.name for entry in os.scandir(wg.WORKFLOWS)}
    parsed = {path.name for path in wg.workflow_files()}
    expected = listing - NOT_WORKFLOWS
    assert parsed == expected, (
        f"read but not listed: {sorted(parsed - expected)}; "
        f"listed but not read: {sorted(expected - parsed)}"
    )
    assert len(parsed) == WORKFLOW_COUNT, (
        f"{len(parsed)} workflows, not {WORKFLOW_COUNT}: a workflow was added or "
        "removed; classify its triggers here and change WORKFLOW_COUNT"
    )


def test_every_workflow_has_triggers_and_only_keys_github_defines(workflows, triggers):
    """`triggers` refuses a workflow with no trigger as it is built."""
    problems = [
        problem
        for name, workflow in sorted(workflows.items())
        for problem in _top_level_problems(name, workflow)
    ]
    assert not problems, "\n".join(problems)


def test_every_trigger_is_classified(triggers):
    unclassified = [
        f"unclassified trigger {trigger} in {name}"
        for name, names in sorted(triggers.items())
        for trigger in sorted(names, key=str)
        if trigger not in ORDINARY | PRIVILEGED
    ]
    assert not unclassified, "\n".join(unclassified)


def test_the_privileged_workflows_are_exactly_these(triggers):
    found = {name for name, names in triggers.items() if names & PRIVILEGED}
    problems = [
        f"unclassified privileged workflow: {name}"
        for name in sorted(found - PRIVILEGED_WORKFLOWS)
    ] + [
        f"no longer privileged: {name} (take it out of PRIVILEGED_WORKFLOWS)"
        for name in sorted(PRIVILEGED_WORKFLOWS - found)
    ]
    assert not problems, "\n".join(problems)


def test_every_privileged_workflow_keeps_the_rules(workflows, triggers):
    privileged = sorted(name for name, names in triggers.items() if names & PRIVILEGED)
    problems = [
        problem
        for name in privileged
        for problem in privileged_problems(name, workflows[name])
    ]
    assert not problems, "\n".join(problems)


def test_no_pull_request_job_names_an_environment(workflows, triggers):
    problems = [
        problem
        for name, names in sorted(triggers.items())
        if "pull_request" in names
        for problem in _environment_problems(name, workflows[name])
    ]
    assert not problems, "\n".join(problems)


# --------------------------------------------------------------------------
# The reader, on each way of writing the trigger key
# --------------------------------------------------------------------------

FORMS = {
    "scalar": ("on: push\n", {"push"}),
    "flow list": ("on: [push, workflow_dispatch]\n", {"push", "workflow_dispatch"}),
    "block list": (
        "on:\n  - push\n  - workflow_dispatch\n",
        {"push", "workflow_dispatch"},
    ),
    "quoted": ('"on": push\n', {"push"}),
    "single-quoted": ("'on': push\n", {"push"}),
    "capitalised": ("On: push\n", {"push"}),
    "anchor and alias": (
        "on:\n  push: &main\n    branches: [main]\n  pull_request: *main\n",
        {"push", "pull_request"},
    ),
    "alias for the whole value": (
        "x: &events [push, schedule]\non: *events\n",
        {"push", "schedule"},
    ),
    "flow map": (
        "on: {push: {branches: [main]}, workflow_dispatch: null}\n",
        {"push", "workflow_dispatch"},
    ),
}


@pytest.mark.parametrize("form", sorted(FORMS))
def test_the_reader_reads_each_form(form, tmp_path):
    text, expected = FORMS[form]
    path = tmp_path / "x.yml"
    path.write_text(text + "jobs: {}\n", encoding="utf-8")
    assert _triggers(path.name, _read(path)) == expected


REFUSED = {
    "both forms of the key": '"on": push\non: workflow_dispatch\n',
    "both forms of the key, the other order": 'on: push\n"on": workflow_dispatch\n',
    "the key twice": "on: push\ntrue: workflow_dispatch\n",
    "two documents": "on: push\n---\non: workflow_dispatch\n",
}


@pytest.mark.parametrize("shape", sorted(REFUSED))
def test_the_loader_refuses_an_ambiguous_trigger_key(shape, tmp_path):
    path = tmp_path / "x.yml"
    path.write_text(REFUSED[shape] + "jobs: {}\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"^x\.yml: cannot be read"):
        _read(path)


def test_triggers_refuses_both_forms_from_any_loader():
    with pytest.raises(ValueError, match="both forms of the trigger key"):
        wg.triggers({"on": "push", True: "workflow_dispatch"})


@pytest.mark.parametrize("text", ["on:\n", "on: []\n", "name: x\n"])
def test_a_workflow_with_no_trigger_fails(text, tmp_path):
    path = tmp_path / "x.yml"
    path.write_text(text + "jobs: {}\n", encoding="utf-8")
    with pytest.raises(AssertionError, match=r"^x\.yml: no trigger"):
        _triggers(path.name, _read(path))


def test_a_top_level_key_github_does_not_define_is_named():
    workflow = {"name": "x", "On": "push", True: "push", "jobs": {}}
    assert _top_level_problems("x.yml", workflow) == [
        "x.yml: top-level key 'On' is not one GitHub defines"
    ]


# --------------------------------------------------------------------------
# Each rule, planted in the real privileged workflow
# --------------------------------------------------------------------------

SHA = "0123456789abcdef0123456789abcdef01234567"
REAL = "dependabot-automerge.yml"


def _steps(workflow: Dict[str, Any]) -> List[Dict[str, Any]]:
    return workflow["jobs"]["automerge"]["steps"]


def _add_step(step: Dict[str, Any]) -> Callable[[Dict[str, Any]], None]:
    return lambda workflow: _steps(workflow).append(step)


def _add_job(job: Dict[str, Any]) -> Callable[[Dict[str, Any]], None]:
    return lambda workflow: workflow["jobs"].__setitem__("planted", job)


def _restore_tag(workflow: Dict[str, Any]) -> None:
    _steps(workflow)[0]["uses"] = "dependabot/fetch-metadata@v3"


def _capitalise_uses(workflow: Dict[str, Any]) -> None:
    step = _steps(workflow)[0]
    del step["uses"]
    step["Uses"] = "dependabot/fetch-metadata@v3"


def _set(*keys: str, value: Any) -> Callable[[Dict[str, Any]], None]:
    def plant(workflow: Dict[str, Any]) -> None:
        target = workflow
        for key in keys[:-1]:
            target = target[key]
        target[keys[-1]] = value

    return plant


PLANTS = {
    "checkout of the head commit": (
        _add_step(
            {
                "uses": f"actions/checkout@{SHA}",
                "with": {"ref": "${{ github.event.pull_request.head.sha }}"},
            }
        ),
        [CHECKOUT, HEAD],
    ),
    "checkout of a pull ref": (
        _add_step(
            {"uses": f"actions/checkout@{SHA}", "with": {"ref": "refs/pull/1/merge"}}
        ),
        [CHECKOUT, HEAD],
    ),
    "checkout of another repository": (
        _add_step(
            {
                "uses": f"actions/checkout@{SHA}",
                "with": {
                    "repository": "${{ github.event.pull_request.head.repo.full_name }}"
                },
            }
        ),
        [CHECKOUT, HEAD],
    ),
    "checkout with a capitalised input name": (
        _add_step({"uses": f"actions/checkout@{SHA}", "with": {"Ref": "${{ env.X }}"}}),
        [CHECKOUT],
    ),
    "a fetch of a pull ref": (
        _add_step({"run": "git fetch origin pull/1/head:pr"}),
        [HEAD, GIT],
    ),
    "a fetch of a head that arrives through env:": (
        _add_step(
            {
                "run": 'git fetch origin "$SHA" && git checkout FETCH_HEAD',
                "env": {"SHA": "${{ github.event.pull_request.head.sha }}"},
            }
        ),
        [GIT],
    ),
    "a plain git fetch": (_add_step({"run": 'git fetch origin "$SHA"'}), [GIT]),
    "git with an option before its command": (
        _add_step({"run": 'git -C . fetch origin "$SHA"'}),
        [GIT],
    ),
    "git with a configuration option": (
        _add_step({"run": 'git -c protocol.version=2 fetch origin "$SHA"'}),
        [GIT],
    ),
    "the CLI's checkout": (_add_step({"run": 'gh pr checkout "$PR"'}), [HEAD, GIT]),
    "a shell variable named after the head": (
        _add_step(
            {
                "run": 'gh pr merge --auto --squash --match-head-commit "$HEAD_SHA"',
                "env": {"HEAD_SHA": "${{ github.event.pull_request.head.sha }}"},
            }
        ),
        [f"{HEAD} in code", "rename the variable"],
    ),
    "a workflow_run head in with:": (
        _add_step(
            {
                "uses": f"example/action@{SHA}",
                "with": {"sha": "${{ github.event.workflow_run.head_sha }}"},
            }
        ),
        [HEAD],
    ),
    "download-artifact": (
        _add_step({"uses": f"actions/download-artifact@{SHA}"}),
        [DOWNLOAD],
    ),
    "gh run download": (_add_step({"run": "gh run download 1"}), [DOWNLOAD]),
    "a named secret": (
        _add_step({"run": "true", "env": {"X": "${{ secrets.X }}"}}),
        [SECRET],
    ),
    "every secret": (
        _add_step({"run": "true", "env": {"X": "${{ toJSON(secrets) }}"}}),
        [SECRET],
    ),
    "secrets inherited by a called workflow": (
        _add_job(
            {
                "uses": "./.github/workflows/dependabot-automerge.yml",
                "secrets": "inherit",
            }
        ),
        [SECRET],
    ),
    "event text in run:": (
        _add_step({"run": 'echo "${{ github.event.pull_request.title }}"'}),
        [EXPRESSION],
    ),
    "event text in a github-script": (
        _add_step(
            {
                "uses": f"actions/github-script@{SHA}",
                "with": {
                    "script": "core.info('${{ github.event.pull_request.title }}')"
                },
            }
        ),
        [EXPRESSION],
    ),
    "github-script with a capitalised input name": (
        _add_step(
            {
                "uses": f"actions/github-script@{SHA}",
                "with": {
                    "Script": "core.info('${{ github.event.pull_request.title }}')"
                },
            }
        ),
        [EXPRESSION],
    ),
    "the tag restored": (_restore_tag, [UNPINNED]),
    "a step key in another case": (_capitalise_uses, [f"{KEY}: 'Uses'"]),
    "a job key in another case": (
        _set("jobs", "automerge", "Environment", value="staging"),
        [f"{KEY}: 'Environment'"],
    ),
    "write-all": (_set("permissions", value="write-all"), [WRITE_ALL]),
    "id-token": (_set("permissions", "id-token", value="write"), [ID_TOKEN]),
    "an environment": (
        _set("jobs", "automerge", "environment", value="staging"),
        [ENVIRONMENT],
    ),
    # Any rule: what matters is that the callee is read and held to them.
    "a called workflow that breaks a rule": (
        _add_job({"uses": "./.github/workflows/_platform.yml"}),
        [f"{REAL}/planted -> _platform.yml/"],
    ),
    "a workflow from another repository": (
        _add_job({"uses": f"example/repo/.github/workflows/x.yml@{SHA}"}),
        [FOREIGN_WORKFLOW],
    ),
    "a local action that breaks a rule": (
        _add_step({"uses": "./.github/actions/licence-environment"}),
        ["-> .github/actions/licence-environment/action.yml step"],
    ),
    "a local action that does not exist": (
        _add_step({"uses": "./.github/actions/absent"}),
        [UNREADABLE],
    ),
}


@pytest.fixture(scope="module")
def real(workflows) -> Dict[str, Any]:
    assert REAL in PRIVILEGED_WORKFLOWS, f"{REAL} is the workflow the plants use"
    return workflows[REAL]


def test_the_real_privileged_workflow_keeps_the_rules(real):
    assert privileged_problems(REAL, real) == []


@pytest.mark.parametrize("plant", sorted(PLANTS))
def test_each_rule_fires(plant, real):
    mutate, expected = PLANTS[plant]
    workflow = copy.deepcopy(real)
    mutate(workflow)
    problems = privileged_problems(REAL, workflow)
    assert problems, f"{plant}: no rule fired"
    for part in expected:
        assert any(part in problem for problem in problems), (plant, part, problems)
    assert all(problem.startswith(REAL) for problem in problems), problems


def test_a_variable_renamed_away_from_head_passes(real):
    """The way out the head rule's message gives: the same value under a name
    that does not begin with head."""
    workflow = copy.deepcopy(real)
    _steps(workflow).append(
        {
            "run": 'gh pr merge --auto --squash --match-head-commit "$PR_SHA"',
            "env": {"PR_SHA": "${{ github.event.pull_request.head.sha }}"},
        }
    )
    assert privileged_problems(REAL, workflow) == []


def test_a_composite_action_that_keeps_the_rules_passes(real):
    """The control for the local-action rule: a composite action that keeps
    every rule may be called."""
    workflow = copy.deepcopy(real)
    _steps(workflow).append({"uses": "./.github/actions/stack-outputs"})
    assert privileged_problems(REAL, workflow) == []


def test_an_environment_in_a_called_workflow_is_named():
    """deploy.yml is not called by anything; it stands in for a called
    workflow whose jobs name environments."""
    workflow = {
        True: "pull_request",
        "jobs": {"a": {"uses": "./.github/workflows/deploy.yml"}},
    }
    problems = _environment_problems("x.yml", workflow)
    prefix = "environment in a pull_request job: x.yml/a -> deploy.yml/"
    assert problems, "no environment found in deploy.yml"
    assert all(problem.startswith(prefix) for problem in problems), problems
