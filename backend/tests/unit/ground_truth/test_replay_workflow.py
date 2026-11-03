"""The replay's workflow (#1544, SPEC section 3.2), and G7.

**G7**: every file in the import closure of ``backend.app.main`` on the core
profile matches a glob of ``ground-truth.yml``'s path filter, read from the
file, so a change to any code the replayed API runs starts the replay. The
closure is computed in a fresh interpreter in which ``import modules`` fails
as it does in a core tree (``backend/app/main.py`` loads the modules at
import), so it is the same in the full checkout and in ``core_build.sh``'s
copy. Removing ``'backend/app/**'`` from the filter fails it, naming
``backend/app/api/deps.py`` among the files no glob covers.

The other tests pin the workflow's settings: ``pull_request`` (never
``pull_request_target``) and ``workflow_dispatch``; read-only permissions and
no secret; one concurrency group per pull request, a newer push cancelling
the run in flight; at most 15 minutes; never a required check; the pinned
paths-filter and the ``e2e`` label; the suite, profile and seed it hands
``_platform.yml``; that ``_platform.yml`` runs the replay for both suites, the
pull-request suite every test file under tests/ground_truth; and the nightly
job, in the failure issue's ``needs``.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import sys

import pytest
import yaml

from backend.tests.unit.ground_truth.library import REPO_ROOT
from backend.tests.unit.infrastructure import _workflow_graph as wg

pytestmark = pytest.mark.unit

WORKFLOW = wg.WORKFLOWS / "ground-truth.yml"
PLATFORM = wg.WORKFLOWS / "_platform.yml"
NIGHTLY = wg.WORKFLOWS / "nightly-qa.yml"
PATHS_FILTER = "dorny/paths-filter@ceb8a2b8f2d89434be7ff52d3de7ec3738c5cc9d"
#: The replay's own files, which the filter names beside the API's closure.
OWN_FILES = (
    "backend/requirements.txt",
    "backend/requirements/**",
    "tests/ground_truth/**",
    ".github/workflows/ground-truth.yml",
    ".github/workflows/_platform.yml",
    "scripts/core_build.sh",
)

#: Run in a fresh interpreter from the repository root: import the API with
#: ``modules`` absent, as a core tree has it, and print every file of the
#: repository the import loaded. ``ModuleNotFoundError`` naming ``modules`` is
#: what ``backend.app.modules_loader`` reads as the core profile.
CLOSURE = r"""
import importlib.abc, json, os, sys

class CoreProfile(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path=None, target=None):
        if name == "modules" or name.startswith("modules."):
            raise ModuleNotFoundError(f"No module named {name!r}", name=name)
        return None

sys.meta_path.insert(0, CoreProfile())
import backend.app.main  # noqa: F401

root = os.path.realpath(sys.argv[1])
files = set()
for module in list(sys.modules.values()):
    origin = getattr(module, "__file__", None)
    if not origin:
        continue
    path = os.path.realpath(origin)
    if path.startswith(root + os.sep):
        files.add(os.path.relpath(path, root).replace(os.sep, "/"))
print("G7-CLOSURE " + json.dumps(sorted(files)))
"""


@functools.lru_cache(maxsize=1)
def closure() -> tuple[str, ...]:
    """The repository files ``import backend.app.main`` loads, core profile."""
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT),
        "APP_ENV": "test",
        "ENVIRONMENT": "test",
        "TESTING": "true",
    }
    result = subprocess.run(
        [sys.executable, "-B", "-c", CLOSURE, str(REPO_ROOT)],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]
    lines = [
        line for line in result.stdout.splitlines() if line.startswith("G7-CLOSURE ")
    ]
    assert len(lines) == 1, result.stdout[-4000:] + result.stderr[-4000:]
    return tuple(json.loads(lines[0][len("G7-CLOSURE ") :]))


def glob_regex(pattern: str) -> re.Pattern[str]:
    """A filter glob as a regular expression: ``**`` any characters, ``*`` and
    ``?`` within one path segment. Syntax this does not model is refused."""
    if any(char in pattern for char in "{}[]!"):
        raise ValueError(f"the filter glob {pattern!r} uses syntax G7 does not model")
    parts = []
    index = 0
    while index < len(pattern):
        if pattern.startswith("**", index):
            parts.append(".*")
            index += 2
        elif pattern[index] == "*":
            parts.append("[^/]*")
            index += 1
        elif pattern[index] == "?":
            parts.append("[^/]")
            index += 1
        else:
            parts.append(re.escape(pattern[index]))
            index += 1
    return re.compile("".join(parts) + r"\Z")


def _workflow() -> dict:
    return wg.load(WORKFLOW)


def filter_globs(workflow: dict) -> list[str]:
    """The ``replay`` filter of the paths-filter step, as written."""
    steps = workflow["jobs"]["decide"]["steps"]
    filters = [
        s for s in steps if str(s.get("uses", "")).startswith("dorny/paths-filter@")
    ]
    assert len(filters) == 1, filters
    parsed = yaml.safe_load(filters[0]["with"]["filters"])
    globs = parsed["replay"]
    assert globs and all(isinstance(glob, str) for glob in globs), globs
    return globs


def uncovered(files: tuple[str, ...], globs: list[str]) -> list[str]:
    patterns = [glob_regex(glob) for glob in globs]
    return [path for path in files if not any(p.match(path) for p in patterns)]


# ---------------------------------------------------------------------------
# G7
# ---------------------------------------------------------------------------
def test_g7_every_file_the_api_imports_is_in_the_filter():
    missing = uncovered(closure(), filter_globs(_workflow()))
    assert not missing, (
        f"G7: {len(missing)} files that backend.app.main imports match no glob of"
        f" ground-truth.yml's filter, so a change to them would not run the replay:"
        f" {missing[:10]}"
    )


def test_g7_the_closure_is_the_core_api():
    """The closure is not vacuous, and it is the core profile's."""
    files = closure()
    assert len(files) > 100, files
    assert "backend/app/main.py" in files
    assert "backend/app/api/deps.py" in files
    assert not [path for path in files if path.startswith("modules/")]


@pytest.mark.parametrize(
    "glob,path,matches",
    [
        ("backend/app/**", "backend/app/api/deps.py", True),
        ("backend/app/**", "backend/application.py", False),
        ("backend/__init__.py", "backend/__init__.py", True),
        ("backend/__init__.py", "backend/__init__.pyc", False),
        ("scripts/*.sh", "scripts/core_build.sh", True),
        ("scripts/*.sh", "scripts/sub/core_build.sh", False),
    ],
)
def test_the_glob_matcher(glob, path, matches):
    assert bool(glob_regex(glob).match(path)) is matches


def test_the_glob_matcher_refuses_what_it_does_not_model():
    with pytest.raises(ValueError):
        glob_regex("backend/{app,lambda}/**")


def test_the_filter_holds_the_replay_s_own_files():
    globs = filter_globs(_workflow())
    assert [glob for glob in OWN_FILES if glob not in globs] == []


# ---------------------------------------------------------------------------
# The workflow's settings (EM-GT-15, PE-GT-12)
# ---------------------------------------------------------------------------
def test_it_runs_on_a_pull_request_and_by_hand_only():
    assert set(wg.triggers(_workflow())) == {"pull_request", "workflow_dispatch"}


def test_adding_the_e2e_label_starts_nothing_by_itself():
    """No ``types:`` on the trigger: the default is opened, synchronize and
    reopened, so a label alone starts no run (a ``labeled`` type would need a
    concurrency group the B3 rule forbids on a label-triggered workflow)."""
    trigger = wg.triggers(_workflow())["pull_request"]
    assert "types" not in (trigger or {})


def test_its_permissions_are_read_only_and_it_reads_no_secret():
    workflow = _workflow()
    assert workflow["permissions"] == {"contents": "read", "pull-requests": "read"}
    for job_id, job in workflow["jobs"].items():
        assert "permissions" not in job, job_id
        assert "secrets" not in job, job_id
    # `secrets.<name>` anywhere, or `secrets: inherit`; the comments' prose
    # ("no secrets.") is not a reference.
    text = WORKFLOW.read_text(encoding="utf-8")
    assert re.findall(r"\bsecrets\.\w+|\bsecrets:\s*inherit", text) == []


def test_one_group_per_pull_request_and_a_newer_push_cancels():
    assert _workflow()["concurrency"] == {
        "group": (
            "ground-truth-${{ github.event_name }}-"
            "${{ github.event.pull_request.number || github.ref }}"
        ),
        "cancel-in-progress": True,
    }


def test_no_job_runs_longer_than_15_minutes():
    jobs = _workflow()["jobs"]
    assert jobs["decide"]["timeout-minutes"] <= 15
    assert jobs["replay"]["with"]["timeout-minutes"] <= 15


def test_it_is_never_a_required_check():
    assert [
        name for name in wg.required_checks() if "ground-truth" in name.lower()
    ] == []


def test_the_decide_job_pins_paths_filter_and_reads_the_e2e_label():
    decide = _workflow()["jobs"]["decide"]
    uses = [step.get("uses") for step in decide["steps"] if step.get("uses")]
    assert uses == [PATHS_FILTER]
    run = next(step for step in decide["steps"] if step.get("id") == "decide")
    assert (
        run["env"]["LABELS"] == "${{ toJSON(github.event.pull_request.labels.*.name) }}"
    )
    assert "'\"e2e\"'" in run["run"]


def test_the_replay_boots_the_core_profile_with_no_seed():
    replay = _workflow()["jobs"]["replay"]
    assert replay["uses"] == "./.github/workflows/_platform.yml"
    assert replay["needs"] == "decide"
    assert replay["if"] == "needs.decide.outputs.run == 'true'"
    assert replay["with"] == {
        "profile": "core",
        "seed": "",
        "suite": "ground-truth",
        "timeout-minutes": 15,
    }


def test_the_platform_runs_the_replay_for_both_suites():
    steps = wg.load(PLATFORM)["jobs"]["platform"]["steps"]
    (run,) = [step["run"] for step in steps if step.get("name") == "Run suite"]
    arm = run.split("ground-truth|ground-truth-nightly)", 1)
    assert len(arm) == 2, "no ground-truth arm in the Run suite case"
    body = arm[1].split(";;", 1)[0]
    assert 'if [ "$SUITE" = ground-truth-nightly ]; then' in body
    assert (
        'GROUND_TRUTH_SCALE="$scale" python -m pytest tests/ground_truth/test_replay.py'
        in body
    )


def _suite_branch(run: str, suite: str) -> str:
    """The body of the "Run suite" case branch whose patterns include *suite*."""
    for match in re.finditer(r"^[ \t]*([\w|-]+)\)[ \t]*$", run, re.M):
        if suite in match.group(1).split("|"):
            return run[match.end() :].split(";;", 1)[0]
    raise AssertionError(f"no case branch of the Run suite step runs {suite!r}")


def test_the_pull_request_suite_runs_every_test_file_of_the_replay():
    """Every ``test_*.py`` under tests/ground_truth is named, or its directory
    is, by a pytest command of the ``ground-truth`` branch: a test file the arm
    does not run is a gate CI never runs."""
    steps = wg.load(PLATFORM)["jobs"]["platform"]["steps"]
    (run,) = [step["run"] for step in steps if step.get("name") == "Run suite"]
    body = _suite_branch(run, "ground-truth")
    paths = [
        path
        for command in re.findall(
            r"python -m pytest((?:\s+tests/ground_truth\S*)+)", body
        )
        for path in command.split()
    ]
    assert paths, body
    files = sorted(
        path.relative_to(REPO_ROOT).as_posix()
        for path in (REPO_ROOT / "tests" / "ground_truth").rglob("test_*.py")
    )
    assert "tests/ground_truth/test_replay.py" in files
    unrun = [
        path
        for path in files
        if not any(path == p or path.startswith(p.rstrip("/") + "/") for p in paths)
    ]
    assert unrun == [], f"the ground-truth arm runs {paths}, not {unrun}"


def test_the_nightly_job_runs_it_and_a_red_night_files_the_issue():
    jobs = wg.load(NIGHTLY)["jobs"]
    assert jobs["ground-truth"]["uses"] == "./.github/workflows/_platform.yml"
    assert jobs["ground-truth"]["with"]["suite"] == "ground-truth-nightly"
    assert jobs["ground-truth"]["with"]["profile"] == "core"
    assert jobs["ground-truth"]["with"]["seed"] == ""
    assert "ground-truth" in jobs["nightly-failure-issue"]["needs"]
