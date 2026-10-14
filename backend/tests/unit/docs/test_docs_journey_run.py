"""A demo document's shell block, run by a journey as its reader runs it (#1142).

``run`` and ``finish`` steps (``tests/acceptance/docs/docs_runner/commands.py``):
a ``run`` step's command is one shell block of its section, word for word, run
from the checkout's root with the checkout's venv active, on demo-local only;
``background: true`` starts it, and a later ``finish`` step checks how it
ended. What is checked here:

* the loader's refusals: a command that is not a block of its section, a run
  off demo-local, a run or finish without ``expect.exit`` or expecting
  something else, a background run with an expect, a background run nobody
  finishes, a finish that names no background run or finishes one twice, and
  ``exit`` or ``output`` on a step of another kind;
* the blocks for real, in a scratch checkout whose ``venv/bin/python`` is this
  interpreter: where they run and with what environment, the exit status and
  the output held to the expect, a block past its bound stopped with its
  whole process group, a block started in the background and finished later,
  and one abandoned when its journey ends.

Temporary files and child processes of this test only; no network, no git.
"""

from __future__ import annotations

import copy
import datetime
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import commands, loader

TODAY = datetime.date(2026, 10, 13)

DOCUMENT = """# A demo

## Setup

Run the script, then open http://localhost:3100.

## The story

From the repository root, with the venv active:

```{.bash skip reason="demo"}
python demo/story.py --auto
```

To run one step:

```bash
python demo/story.py --step 1
```

## Nothing to run

Just prose.
"""

RUN_STEP: Dict[str, Any] = {
    "id": "story",
    "doc": "the-story",
    "do": {"run": {"command": "python demo/story.py --auto", "up_to_seconds": 60}},
    "expect": {"exit": 0, "output": ["Step 7/7"]},
    "fail": "the story does not run to its end",
}

JOURNEY: Dict[str, Any] = {
    "guide": "demo/GUIDE.md",
    "stack": "demo-local",
    "profile": "full",
    "video": False,
    "written": TODAY,
    "steps": [RUN_STEP],
}

IN_SCOPE = {"scope": "in", "class": "journey", "journey": "guide", "reason": "walked"}


def _load(tmp_path: Path, data: Dict[str, Any]):
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "demo").mkdir(exist_ok=True)
    (tmp_path / "demo" / "GUIDE.md").write_text(DOCUMENT, encoding="utf-8")
    context = loader.Context(
        docs_root=tmp_path / "docs",
        inventory={"pages": {}, "documents": {"demo/GUIDE.md": IN_SCOPE}},
        doc_examples={},
        oracles={},
        today=TODAY,
    )
    path = tmp_path / "journeys" / "guide.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return loader.load(path, context)


def _refusals(tmp_path: Path, data: Dict[str, Any]) -> list:
    with pytest.raises(loader.Refused) as refused:
        _load(tmp_path, data)
    return refused.value.problems


def _with_steps(*steps: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
    data = copy.deepcopy(JOURNEY)
    data["steps"] = [copy.deepcopy(step) for step in steps]
    data.update(fields)
    return data


def _background(step_id: str = "story") -> Dict[str, Any]:
    step = copy.deepcopy(RUN_STEP)
    step["id"] = step_id
    step["do"]["run"]["background"] = True
    del step["expect"]
    return step


def _finish(name: str = "story", step_id: str = "story-ended") -> Dict[str, Any]:
    return {
        "id": step_id,
        "doc": "the-story",
        "do": {"finish": name},
        "expect": {"exit": 0},
        "fail": "the story does not end well",
    }


# ---------------------------------------------------------------------------
# The loader
# ---------------------------------------------------------------------------
def test_a_run_of_a_block_of_its_section_loads(tmp_path):
    journey = _load(tmp_path, JOURNEY)
    assert journey.steps[0].do.run.command == "python demo/story.py --auto"
    started = _load(tmp_path, _with_steps(_background(), _finish()))
    assert [step.do.kind for step in started.steps] == ["run", "finish"]


@pytest.mark.parametrize(
    "command",
    [
        "python demo/story.py",
        "python demo/story.py --auto --pace 20",
        "python demo/story.py  --auto",
    ],
    ids=["a-prefix-of-a-block", "a-longer-command", "not-word-for-word"],
)
def test_a_command_that_is_not_a_block_of_its_section_is_refused(tmp_path, command):
    step = copy.deepcopy(RUN_STEP)
    step["do"]["run"]["command"] = command
    problems = _refusals(tmp_path, _with_steps(step))
    assert any(
        "run takes a shell block of #the-story as the page writes it" in problem
        for problem in problems
    ), problems


def test_a_block_of_another_section_is_refused(tmp_path):
    step = copy.deepcopy(RUN_STEP)
    step["doc"] = "nothing-to-run"
    problems = _refusals(tmp_path, _with_steps(step))
    assert any("the section has no shell block" in p for p in problems), problems


def test_a_run_off_demo_local_is_refused(tmp_path):
    problems = _refusals(tmp_path, _with_steps(RUN_STEP, stack="compose-dev"))
    assert any(
        "a run step runs a block of the guide in the checkout" in p
        and "compose-dev has none" in p
        for p in problems
    ), problems


@pytest.mark.parametrize(
    "expect, message",
    [
        ({"output": ["Step 7/7"]}, "a run step needs expect.exit"),
        ({"exit": 0, "status": 200}, "a run step expects only exit or output"),
        ({"exit": 0, "json": {"a": 1}}, "a run step expects only exit or output"),
    ],
    ids=["no-exit", "status", "json"],
)
def test_a_run_expects_its_exit_and_output_only(tmp_path, expect, message):
    step = copy.deepcopy(RUN_STEP)
    step["expect"] = expect
    problems = _refusals(tmp_path, _with_steps(step))
    assert any(message in p for p in problems), problems


def test_a_run_has_no_screen_to_snapshot(tmp_path):
    step = copy.deepcopy(RUN_STEP)
    step["snapshot"] = True
    problems = _refusals(tmp_path, _with_steps(step))
    assert any("a run step has no screen to snapshot" in p for p in problems)


def test_a_background_run_with_an_expect_is_refused(tmp_path):
    step = _background()
    step["expect"] = {"exit": 0}
    problems = _refusals(tmp_path, _with_steps(step, _finish()))
    assert any("a background run is checked by the finish step" in p for p in problems)


def test_a_background_run_nobody_finishes_is_refused(tmp_path):
    problems = _refusals(tmp_path, _with_steps(_background()))
    assert any(
        "a background run needs a later finish step that names it ('story')" in p
        for p in problems
    ), problems


@pytest.mark.parametrize(
    "steps, message",
    [
        ([_finish("story", "early"), _background()], "names no earlier run step"),
        ([RUN_STEP, _finish()], "names no earlier run step"),
        (
            [_background(), _finish(), _finish("story", "again")],
            "finish 'story' is finished twice",
        ),
    ],
    ids=["before-the-run", "a-run-not-in-the-background", "twice"],
)
def test_a_finish_names_one_earlier_background_run(tmp_path, steps, message):
    problems = _refusals(tmp_path, _with_steps(*steps))
    assert any(message in p for p in problems), problems


def test_a_finish_needs_its_exit(tmp_path):
    finish = _finish()
    finish["expect"] = {"output": ["done"]}
    problems = _refusals(tmp_path, _with_steps(_background(), finish))
    assert any("a finish step needs expect.exit" in p for p in problems), problems


def test_exit_and_output_belong_to_run_and_finish_only(tmp_path):
    api = {
        "id": "health",
        "doc": "setup",
        "do": {"api": {"method": "GET", "path": "/health", "as": "anonymous"}},
        "expect": {"status": 200, "exit": 0},
        "fail": "the API does not answer",
    }
    problems = _refusals(tmp_path, _with_steps(api))
    assert any("an api step expects only status or json" in p for p in problems)


# ---------------------------------------------------------------------------
# The blocks, run for real in a scratch checkout
# ---------------------------------------------------------------------------
@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    """A checkout whose venv's python is this interpreter."""
    root = tmp_path / "checkout"
    (root / "venv" / "bin").mkdir(parents=True)
    (root / "venv" / "bin" / "python").symlink_to(sys.executable)
    return root


BASE = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": "/nonexistent"}


def test_block_problem_takes_a_whole_block_only():
    section = DOCUMENT.split("## The story", 1)[1].split("## Nothing", 1)[0]
    assert commands.shell_blocks(section) == [
        "python demo/story.py --auto",
        "python demo/story.py --step 1",
    ]
    assert commands.block_problem(section, "python demo/story.py --step 1") is None
    assert commands.block_problem(section, "python demo/story.py --step 1\n") is None
    assert "not one of the section's shell blocks" in commands.block_problem(
        section, "python demo/story.py --step"
    )
    assert (
        commands.block_problem("prose only", "ls") == "the section has no shell block"
    )


def test_a_block_runs_from_the_checkout_with_its_venv_active(checkout):
    finished = commands.run(
        'pwd; printf "venv=%s\\n" "$VIRTUAL_ENV"; command -v python',
        checkout,
        30,
        BASE,
    )
    assert finished.status == 0, finished.output
    lines = finished.output.splitlines()
    assert Path(lines[0]).resolve() == checkout.resolve()
    assert lines[1] == f"venv={checkout / 'venv'}"
    assert lines[2] == str(checkout / "venv" / "bin" / "python")
    assert commands.judge(finished, 0, [f"venv={checkout / 'venv'}"]) == []


def test_a_block_sees_nothing_of_the_runners_environment_but_home_and_path(checkout):
    base = {**BASE, "DOCS_JOURNEY_PLANTED": "a-value-the-runner-holds"}
    finished = commands.run("env", checkout, 30, base)
    assert finished.status == 0
    assert "a-value-the-runner-holds" not in finished.output
    names = {
        line.split("=", 1)[0] for line in finished.output.splitlines() if "=" in line
    }
    assert {"PATH", "HOME", "VIRTUAL_ENV"} <= names
    assert "DOCS_JOURNEY_PLANTED" not in names


def test_the_exit_status_and_the_output_are_held_to_the_expect(checkout):
    finished = commands.run("printf 'Step 3/7\\n'; exit 3", checkout, 30, BASE)
    assert finished.status == 3
    assert commands.judge(finished, 3, ["Step 3/7"]) == []
    assert commands.judge(finished, 0, ["Step 3/7", "Step 7/7"]) == [
        "it exited 3, not 0",
        "its output has no 'Step 7/7'",
    ]


def test_a_block_stops_at_its_first_failing_line(checkout):
    finished = commands.run("false\nprintf after", checkout, 30, BASE)
    assert finished.status == 1
    assert "after" not in finished.output


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_a_block_past_its_bound_is_stopped_with_its_process_group(checkout):
    finished = commands.run(
        "sleep 60 & printf '%s' $! > child.pid; wait", checkout, 1, BASE
    )
    assert finished.stopped and finished.status is None
    problems = commands.judge(finished, 0, [])
    assert len(problems) == 1 and problems[0].startswith("it had not ended after")
    child = int((checkout / "child.pid").read_text())
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not _alive(child), "the block's own child outlived the stop"


def test_a_block_started_in_the_background_is_finished_later(checkout):
    started = commands.start(
        "printf 'started\\n'; while [ ! -f go ]; do sleep 0.1; done; printf finished",
        checkout,
        30,
        BASE,
    )
    assert started.process.poll() is None
    (checkout / "go").write_text("", encoding="utf-8")
    finished = commands.finish(started)
    assert finished.status == 0
    assert commands.judge(finished, 0, ["started", "finished"]) == []
    assert not started.output.exists(), "the raw output file is left behind"


def test_a_block_whose_finish_never_comes_is_abandoned(checkout):
    started = commands.start("printf 'so far\\n'; sleep 60", checkout, 600, BASE)
    pid = started.process.pid
    deadline = time.monotonic() + 10
    while b"so far" not in started.output.read_bytes() and time.monotonic() < deadline:
        time.sleep(0.05)
    ended = commands.abandon(started)
    assert started.process.poll() is not None
    assert not _alive(pid)
    assert ended.stopped and ended.status is None
    assert "so far" in ended.output
    assert not started.output.exists()


def test_a_checkout_without_a_venv_cannot_run_a_block(tmp_path):
    with pytest.raises(commands.CommandError, match="no venv/bin"):
        commands.start("true", tmp_path, 30, BASE)
    with pytest.raises(commands.CommandError, match="no checkout"):
        commands.start("true", None, 30, BASE)


def test_the_output_kept_is_cut_to_its_end():
    finished = commands.Finished(
        status=0, seconds=1.0, output="x" * commands.OUTPUT_CHARS + "the end"
    )
    tail = finished.tail()
    assert tail.endswith("the end") and tail.startswith("...")
    assert len(tail) == commands.OUTPUT_CHARS + 3
