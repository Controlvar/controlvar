"""The use cases' workflow: ``_platform.yml``'s ``usecases-flags`` arm and
``nightly-qa.yml``'s ``flags-over-time`` job, pinned as written.

* **The arm** is exactly two command lines: the JS SDK's build, on a line of
  its own (under ``set -euo pipefail`` a failed ``(cd ... && ...)`` there ends
  the step; chained in front of pytest as ``(build) && pytest`` it fails open
  once any line follows), then the whole ``tests/usecases`` root under
  ``timeout 15m`` (exit 124 is a failure, so a hung run still files the
  nightly issue), with ``--tb=short`` and never ``-l``/``--showlocals`` (the
  log is public). ``arm_problems`` is checked against planted forms of the
  arm too, so the pin itself is shown to fire.
* **The nightly job** hands ``_platform.yml`` the core profile, no seed, the
  suite, ``python js`` (which sets up Node) and a 20-minute limit that the
  suite's own 15 minutes fit inside, and it is in the failure issue's
  ``needs``.

The arm adds no step, input or ``if:`` (``test_docs_only_gate.py`` pins every
step and its condition); the input's description names the suite.
"""

from __future__ import annotations

import re

import pytest

from backend.tests.unit.infrastructure import _workflow_graph as wg
from backend.tests.unit.usecases.harness import sdks

pytestmark = pytest.mark.unit

PLATFORM = wg.WORKFLOWS / "_platform.yml"
NIGHTLY = wg.WORKFLOWS / "nightly-qa.yml"
SUITE = "usecases-flags"
JOB = "flags-over-time"
BUILD = (
    "(cd sdk/js && npm ci --ignore-scripts --no-audit --no-fund"
    " && npm run build --silent)"
)
RUN = "timeout 15m python -P -m pytest tests/usecases -p no:cov -v --tb=short"
#: The job's minutes beyond the suite's own limit: boot and build (measured
#: about two minutes for the boot on the ground-truth job).
BOOT_MINUTES = 3


def run_suite() -> str:
    steps = wg.load(PLATFORM)["jobs"]["platform"]["steps"]
    (run,) = [step["run"] for step in steps if step.get("name") == "Run suite"]
    return run


def arm_lines(run: str) -> list[str] | None:
    """The command lines of the ``usecases-flags)`` arm, comments and blank
    lines left out; ``None`` when there is no such arm."""
    lines = run.splitlines()
    starts = [i for i, line in enumerate(lines) if line.strip() == f"{SUITE})"]
    if len(starts) != 1:
        return None
    body = []
    for line in lines[starts[0] + 1 :]:
        text = line.strip()
        if text == ";;":
            return body
        if text and not text.startswith("#"):
            body.append(text)
    return None


def arm_problems(run: str) -> list[str]:
    """What is wrong with the arm in *run* (Run suite's script)."""
    found = []
    first = next((line.strip() for line in run.splitlines() if line.strip()), "")
    if first != "set -euo pipefail":
        found.append(f"Run suite starts with {first!r}, not set -euo pipefail")
    lines = arm_lines(run)
    if lines is None:
        return [*found, f"no single {SUITE}) arm ending in ;;"]
    if lines != [BUILD, RUN]:
        found.append(f"the arm runs {lines}, expected exactly {[BUILD, RUN]}")
    tokens = " ".join(lines).split()
    if "-l" in tokens or any(t.startswith("--showlocals") for t in tokens):
        found.append("the arm shows frames' local variables in a public log")
    return found


# -- the arm ---------------------------------------------------------------------------
def test_the_arm_builds_on_its_own_line_then_runs_the_whole_root_bounded():
    assert arm_problems(run_suite()) == []


def _planted(run: str, plant: str) -> str:
    """*run* with the arm's two lines changed as *plant* names."""
    lines = run.splitlines(keepends=True)
    (at,) = [i for i, line in enumerate(lines) if line.strip() == BUILD]
    indent = lines[at][: len(lines[at]) - len(lines[at].lstrip())]
    pytest_only = RUN.removeprefix("timeout 15m ")
    changed = {
        # PE-S2B-2: chained in front of pytest, with a line after it.
        "chained-with-a-trailing-line": [
            f"{indent}{BUILD} && {pytest_only}\n",
            f"{indent}echo done\n",
        ],
        "trailing-line": [lines[at], lines[at + 1], f"{indent}echo done\n"],
        "showlocals": [lines[at], f"{indent}{RUN} -l\n"],
        "one-file": [
            lines[at],
            f"{indent}{RUN.replace('tests/usecases', 'tests/usecases/test_flags_over_time.py')}\n",
        ],
        "no-timeout": [lines[at], f"{indent}{pytest_only}\n"],
        "set-e-dropped": [lines[at], lines[at + 1]],
    }[plant]
    lines[at : at + 2] = changed
    if plant == "set-e-dropped":
        lines = [line for line in lines if line.strip() != "set -euo pipefail"]
    return "".join(lines)


@pytest.mark.parametrize(
    "plant",
    [
        "chained-with-a-trailing-line",
        "trailing-line",
        "showlocals",
        "one-file",
        "no-timeout",
        "set-e-dropped",
    ],
)
def test_the_arm_pin_fires_on_a_planted_form(plant):
    run = run_suite()
    planted = _planted(run, plant)
    assert planted != run
    assert arm_problems(planted) != []


def test_the_build_is_the_one_the_cases_name_when_dist_is_missing():
    assert BUILD == sdks.JS_BUILD
    assert "npm ci " in BUILD and "--ignore-scripts" in BUILD


def test_the_suite_is_named_in_the_input_and_the_known_message():
    platform = wg.load(PLATFORM)
    inputs = wg.triggers(platform)["workflow_call"]["inputs"]
    assert SUITE in inputs["suite"]["description"]
    known = [line for line in run_suite().splitlines() if "Known:" in line]
    assert len(known) == 1 and SUITE in known[0]


# -- the nightly job ----------------------------------------------------------------------
def test_the_nightly_job_hands_the_platform_its_inputs():
    job = wg.load(NIGHTLY)["jobs"][JOB]
    assert job["uses"] == "./.github/workflows/_platform.yml"
    assert job["with"] == {
        "profile": "core",
        "seed": "",
        "suite": SUITE,
        "sdks": "python js",
        "timeout-minutes": 20,
    }


def test_a_red_or_hung_night_files_the_issue():
    jobs = wg.load(NIGHTLY)["jobs"]
    issue = jobs["nightly-failure-issue"]
    assert JOB in wg.needs_of(issue)
    # A hung night ends `cancelled` (its timeout), not `failure` (#1695).
    assert "contains(needs.*.result, 'failure')" in issue["if"]
    assert "contains(needs.*.result, 'cancelled')" in issue["if"]
    (minutes,) = re.findall(r"^timeout (\d+)m ", RUN)
    assert int(minutes) + BOOT_MINUTES <= jobs[JOB]["with"]["timeout-minutes"]


def test_the_job_sets_up_node():
    steps = wg.load(PLATFORM)["jobs"]["platform"]["steps"]
    (node,) = [step for step in steps if step.get("name") == "Set up Node.js"]
    assert "contains(inputs.sdks, 'js')" in node["if"]
    sdks_input = wg.load(NIGHTLY)["jobs"][JOB]["with"]["sdks"]
    assert "js" in sdks_input.split()
