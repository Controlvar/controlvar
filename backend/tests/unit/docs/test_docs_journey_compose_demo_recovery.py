"""The compose-demo journey's recovery leg: the page's recovery note, walked (D73, PR-3b).

``docs/getting-started/docker-compose-file.md`` says: "If you already started
the profile without them, run the command above once: it applies the missing
seeds to the existing database, with no down -v." The journey's ``relaunch:
recovery`` step (``docs_runner/stacks.py``, ``ComposeDemo.relaunch``) brings
compose-demo down with its volumes, starts it with the page's demo command
less its ``SEED=``, checks that the apps' key files are absent, then runs the
page's command as written, and the first leg's checks follow. What decides
whether that walk can pass for the wrong reason is held here, in the unit
job, with a fake ``docker`` on ``PATH``:

* the start without the seeds is the page's command with its leading
  ``SEED=`` taken off and nothing else, run with no ``SEED`` in its
  environment; a command that does not begin with ``SEED=``, or sets it
  again, is refused;
* the order: ``down -v``, that start (its exit status not held), the key
  files checked absent, the page's command (exit 0 held), with no ``down -v``
  between the two starts; neither start removes or builds an image;
* key files that are present, or that docker cannot read, after the start
  without the seeds fail the step before the page's command runs: the leg
  would otherwise recover nothing and pass;
* a ``relaunch`` step is refused on every stack but compose-demo (compose-dev
  and demo-local named), and with an ``expect`` or a ``snapshot``;
* the runner writes what the relaunch did, drops the accounts' sign-ins (the
  ``down -v`` made the accounts anew) and fails the step on its problem;
* the leg after it repeats the first leg's checks, step for step.

Temporary files only; no network, no git, no Docker, nothing from
``modules/``, so it runs the same in ``scripts/core_build.sh``'s copy.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import types
from pathlib import Path
from typing import Any, Dict, List

import pytest
import yaml
from pydantic import ValidationError

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import guide as guides
from docs_runner import loader, services, stacks
from docs_runner.checks import describe_action, describe_expect
from docs_runner.model import Do
from docs_runner.stacks import ComposeDemo, Running, StackError

JOURNEY = RUNNER_ROOT / "journeys" / "compose-demo.yaml"
PAGE = "getting-started/docker-compose-file.md"
PAGE_COMMAND = (
    "SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait"
)
BARE = "docker compose --profile demo up -d --wait"
SHOPLAB_KEY, STREAMPULSE_KEY = stacks.COMPOSE_DEMO_SEEDED_FILES


def _load(path: Path = JOURNEY):
    return loader.load(path, loader.context_for(REPO_ROOT))


def _journey_data() -> Dict[str, Any]:
    return yaml.safe_load(JOURNEY.read_text(encoding="utf-8"))


def _planted(tmp_path: Path, data: Dict[str, Any]) -> Path:
    path = tmp_path / "compose-demo.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _refusals(path: Path) -> List[str]:
    with pytest.raises(loader.Refused) as refused:
        _load(path)
    return refused.value.problems


def _recovery_index(data: Dict[str, Any]) -> int:
    ids = [step["id"] for step in data["steps"]]
    return ids.index("recovery")


# ---------------------------------------------------------------------------
# The journey: the recovery note, then the first leg's checks again
# ---------------------------------------------------------------------------
def test_the_recovery_walks_the_note_the_page_gives():
    journey = _load()
    recovery = next(step for step in journey.steps if step.id == "recovery")
    assert recovery.do.relaunch == "recovery"
    page = guides.load(REPO_ROOT / "docs", PAGE)
    assert recovery.doc in page.anchors
    assert page.contains(
        "If you already started the profile without them, run the command above"
        " once: it applies the missing seeds to the existing database, with no"
        " `down -v`."
    )


def test_the_leg_after_the_recovery_repeats_the_first_legs_checks():
    """Step for step: the same action, the same expectations, so the recovered
    stack is held to everything the first start was."""
    data = _journey_data()
    at = _recovery_index(data)
    first, second = data["steps"][:at], data["steps"][at + 1 :]
    assert len(first) == len(second) == 6
    for before, after in zip(first, second):
        assert after["id"] == f"recovered-{before['id']}"
        assert after["do"] == before["do"]
        assert after.get("expect") == before.get("expect")
        assert after["doc"] == before["doc"]
    assert first[0]["do"]["services"]["for_seconds"] == 60


# ---------------------------------------------------------------------------
# A relaunch step: where it may be, and what it may say
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stack", ["compose-dev", "demo-local"])
def test_a_relaunch_step_is_refused_on_any_other_stack(tmp_path, stack):
    data = _journey_data()
    data["stack"] = stack
    problems = _refusals(_planted(tmp_path, data))
    assert any(
        "(recovery): a relaunch step walks the recovery note" in p
        and f"{stack} is not that stack" in p
        for p in problems
    ), problems


@pytest.mark.parametrize(
    "extra, refused",
    [
        pytest.param(
            {"expect": {"status": 200}},
            "relaunch checks what the action says",
            id="an expect",
        ),
        pytest.param(
            {"snapshot": True},
            "a relaunch step has no screen to snapshot",
            id="a snapshot",
        ),
    ],
)
def test_a_relaunch_step_checks_only_what_its_action_says(tmp_path, extra, refused):
    data = _journey_data()
    data["steps"][_recovery_index(data)].update(extra)
    problems = _refusals(_planted(tmp_path, data))
    assert any(refused in p for p in problems), problems


def test_recovery_is_the_one_relaunch():
    with pytest.raises(ValidationError, match="Input should be 'recovery'"):
        Do.model_validate({"relaunch": "restart"})
    assert Do.model_validate({"relaunch": "recovery"}).kind == "relaunch"


def test_the_step_says_what_it_does_and_what_it_holds():
    step = next(step for step in _load().steps if step.id == "recovery")
    assert describe_action(step) == (
        "bring the stack down with its volumes, start it with the page's demo"
        " command less its SEED=, then run the page's command once more, with no"
        " down -v between"
    )
    assert describe_expect(step) == (
        "after the start without the seeds the apps' key files are absent; the"
        " page's command then exits 0 and the API reports the journey's profile"
    )


# ---------------------------------------------------------------------------
# The start without the seeds
# ---------------------------------------------------------------------------
def test_the_start_without_the_seeds_is_the_page_command_less_its_seed():
    assert stacks.demo_command(REPO_ROOT) == PAGE_COMMAND
    assert stacks.without_seeds(PAGE_COMMAND) == BARE


@pytest.mark.parametrize(
    "command, refused",
    [
        (BARE, "does not begin with SEED="),
        ("export SEED=demo; " + BARE, "does not begin with SEED="),
        ("SEED=demo SEED=demo,shoplab " + BARE, "still sets SEED"),
    ],
)
def test_a_command_the_start_cannot_be_taken_from_is_refused(command, refused):
    with pytest.raises(StackError, match=refused):
        stacks.without_seeds(command)


def test_the_key_files_are_the_ones_the_seeds_write_and_the_simulators_wait_for():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    api = compose["services"]["api"]["environment"]
    assert f"{api['SHOPLAB_DEMO_DIR']}/.api_key" == SHOPLAB_KEY
    assert f"{api['STREAMPULSE_DEMO_DIR']}/.api_key" == STREAMPULSE_KEY
    for app, key in (("shoplab", SHOPLAB_KEY), ("streampulse", STREAMPULSE_KEY)):
        command = " ".join(compose["services"][f"{app}-simulator"]["command"])
        assert f"test -f {key}" in command


# ---------------------------------------------------------------------------
# The relaunch, against a fake docker
# ---------------------------------------------------------------------------
FAKE_DOCKER = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
seed = os.environ.get("SEED", "unset")
with open(os.path.join(here, "calls.log"), "a") as log:
    log.write(" ".join(args) + " | SEED=" + seed + "\\n")
path = os.path.join(here, "state.json")
with open(path) as handle:
    state = json.load(handle)
if args[:1] == ["compose"]:
    words, rest = [], args[1:]
    while rest:
        if rest[0] in ("-p", "--profile"):
            rest = rest[2:]
            continue
        words.append(rest.pop(0))
    command = words[0] if words else ""
    if command == "config":
        print("\\n".join(state.get("images", [])))
    elif command == "up":
        sys.exit(state.get("bare_exit" if seed == "unset" else "up_exit", 0))
    elif command == "exec":
        found = state.get("files", {{}}).get(words[-1], "absent")
        if found == "absent":
            print("absent")
        elif found != "present":
            sys.exit(int(found))
    sys.exit(0)
if args[:2] == ["image", "rm"]:
    state.setdefault("removed", []).append(args[-1])
    with open(path, "w") as handle:
        json.dump(state, handle)
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    sys.exit(1 if args[-1] in state.get("removed", []) else 0)
sys.exit(0)
"""
COMPOSE_FILE = """services:
  api:
    ports:
      - "127.0.0.1:${API_HOST_PORT:-8000}:8000"
  frontend:
    ports:
      - "127.0.0.1:${FRONTEND_HOST_PORT:-3000}:8080"
  shoplab:
    ports:
      - "127.0.0.1:${SHOPLAB_HOST_PORT:-3200}:3200"
  streampulse:
    ports:
      - "127.0.0.1:${STREAMPULSE_HOST_PORT:-3300}:3300"
"""
IMAGES = ["a-api:docs-journeys-demo-core", "postgres:16-alpine"]
DEMO = "compose -p docs-journeys-demo --profile demo"
DOWN = f"{DEMO} down -v --remove-orphans | SEED=unset"
STARTED_BARE = "compose --profile demo up -d --wait | SEED=unset"
STARTED = "compose --profile demo up -d --wait | SEED=demo,shoplab,streampulse"


def _exec(path: str) -> str:
    return f"{DEMO} exec -T api sh -c {services.FILE_TEST} {path} | SEED=unset"


@pytest.fixture
def tree(tmp_path) -> Path:
    """A repository with the compose file and the real page, and a fake docker."""
    root = tmp_path / "repo"
    (root / "docs" / "getting-started").mkdir(parents=True)
    (root / "docker-compose.yml").write_text(COMPOSE_FILE)
    shutil.copy(REPO_ROOT / "docs" / PAGE, root / "docs" / PAGE)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "docker").write_text(FAKE_DOCKER.format(python=sys.executable))
    (bin_dir / "docker").chmod(0o755)
    _state(root, {"images": IMAGES})
    return root


@pytest.fixture(autouse=True)
def _no_stack_left_up():
    yield
    stacks._COMPOSE_UP.clear()


def _state(tree: Path, state: Dict[str, Any]) -> None:
    (tree.parent / "bin" / "state.json").write_text(json.dumps(state))


def _calls(tree: Path) -> List[str]:
    log = tree.parent / "bin" / "calls.log"
    return log.read_text().splitlines() if log.is_file() else []


def _environ(tree: Path) -> Dict[str, str]:
    return {"PATH": f"{tree.parent / 'bin'}{os.pathsep}/usr/bin{os.pathsep}/bin"}


def _up(tree: Path, monkeypatch, served: str = "core") -> ComposeDemo:
    stack = ComposeDemo(tree, tree.parent / "run" / "stacks", _environ(tree))
    monkeypatch.setattr(stack, "_served_profile", lambda api_url: served)
    stack.up("core")
    return stack


def _relaunched(tree: Path, monkeypatch, state: Dict[str, Any], served="core"):
    stack = _up(tree, monkeypatch)
    _state(tree, {"images": IMAGES, **state})
    before = len(_calls(tree))
    monkeypatch.setattr(stack, "_served_profile", lambda api_url: served)
    record = stack.running.relaunch("recovery")
    return stack, record, _calls(tree)[before:]


def test_the_relaunch_clears_starts_without_seeds_checks_then_runs_the_page_command(
    tree, monkeypatch
):
    stack, record, calls = _relaunched(tree, monkeypatch, {})
    assert calls == [
        DOWN,
        STARTED_BARE,
        _exec(SHOPLAB_KEY),
        _exec(STREAMPULSE_KEY),
        STARTED,
    ]
    assert record["problem"] == ""
    assert record["without_seeds"]["command"] == BARE
    assert record["command"]["command"] == PAGE_COMMAND
    assert record["seeded_files"] == {SHOPLAB_KEY: "absent", STREAMPULSE_KEY: "absent"}
    assert record["profile"] == "core"
    # The stack is the one it was: the same addresses, its relaunch still there.
    assert stack.running is not None and stack.running.relaunch == stack.relaunch


def test_no_down_and_no_image_change_between_the_two_starts(tree, monkeypatch):
    _, _, calls = _relaunched(tree, monkeypatch, {})
    between = calls[calls.index(STARTED_BARE) + 1 : calls.index(STARTED)]
    assert not [call for call in between if " down" in call or "image" in call]
    assert not [
        call for call in calls if call.startswith("image ") or "--build" in call
    ]


def test_how_the_start_without_the_seeds_ends_is_not_held(tree, monkeypatch):
    """With a simulator looping, --wait may say so: a reader's start fails."""
    _, record, calls = _relaunched(tree, monkeypatch, {"bare_exit": 1})
    assert record["problem"] == ""
    assert record["without_seeds"]["exit"] == 1
    assert calls[-1] == STARTED


@pytest.mark.parametrize(
    "files, left",
    [
        pytest.param(
            {SHOPLAB_KEY: "present"},
            f"{SHOPLAB_KEY} present",
            id="a key written: the start had its seed",
        ),
        pytest.param(
            {STREAMPULSE_KEY: 1},
            f"{STREAMPULSE_KEY} unreadable (exit 1)",
            id="docker cannot say",
        ),
    ],
)
def test_a_start_that_left_nothing_to_recover_fails_before_the_page_command(
    tree, monkeypatch, files, left
):
    _, record, calls = _relaunched(tree, monkeypatch, {"files": files})
    assert record["problem"] == (
        f"after the start without the seeds, {left}: the recovery would recover nothing"
    )
    assert STARTED not in calls


def test_a_page_command_that_fails_on_the_recovered_stack_fails_the_relaunch(
    tree, monkeypatch
):
    _, record, _ = _relaunched(tree, monkeypatch, {"up_exit": 1})
    assert record["problem"] == (
        f"the page's demo command ({PAGE_COMMAND}) exited 1 on the stack started"
        " without the seeds; its output is in stacks/compose-demo.log"
    )


def test_a_stack_serving_another_profile_after_the_recovery_fails_it(tree, monkeypatch):
    _, record, _ = _relaunched(tree, monkeypatch, {}, served="full")
    assert record["problem"] == "the stack serves the full profile, not core"


def test_only_a_stack_that_is_up_relaunches_and_only_as_recovery(tree, monkeypatch):
    stack = ComposeDemo(tree, tree.parent / "run" / "stacks", _environ(tree))
    assert stack.relaunch("recovery")["problem"] == "the compose-demo stack is not up"
    up = _up(tree, monkeypatch)
    assert up.relaunch("restart")["problem"] == (
        "no relaunch 'restart'; recovery is the only one"
    )


def test_the_start_without_the_seeds_is_bounded_by_its_own_time(tree, monkeypatch):
    stack = _up(tree, monkeypatch)
    assert stack.bare_timeout == stacks.DEMO_BARE_TIMEOUT == 300
    bounds = {}
    real = stacks._run_logged

    def spy(argv, **kwargs):
        if argv[:2] == ["sh", "-c"]:
            bounds[argv[2]] = kwargs["timeout"]
        return real(argv, **kwargs)

    monkeypatch.setattr(stacks, "_run_logged", spy)
    assert stack.relaunch("recovery")["problem"] == ""
    assert bounds == {BARE: 300.0, PAGE_COMMAND: 1500.0}
    moved = ComposeDemo(
        tree,
        tree.parent / "run",
        {**_environ(tree), "DOCS_JOURNEY_DEMO_BARE_TIMEOUT": "120"},
    )
    assert moved.bare_timeout == 120.0


# ---------------------------------------------------------------------------
# The runner's step (execute.py), with a stand-in for Playwright
# ---------------------------------------------------------------------------
@pytest.fixture
def execute_module(monkeypatch):
    """docs_runner.execute with a stand-in for Playwright, which the unit job lacks."""
    import importlib

    sync_api = types.ModuleType("playwright.sync_api")

    class _Expect:
        def set_options(self, **kwargs):
            return None

    sync_api.Error = type("Error", (Exception,), {})
    sync_api.Browser = sync_api.Page = sync_api.Playwright = object
    sync_api.expect = _Expect()
    package = types.ModuleType("playwright")
    package.sync_api = sync_api
    monkeypatch.setitem(sys.modules, "playwright", package)
    monkeypatch.setitem(sys.modules, "playwright.sync_api", sync_api)
    monkeypatch.delitem(sys.modules, "docs_runner.execute", raising=False)
    module = importlib.import_module("docs_runner.execute")
    yield module
    sys.modules.pop("docs_runner.execute", None)


DONE = {
    "relaunch": "recovery",
    "problem": "",
    "cleared": "down -v --remove-orphans",
    "without_seeds": {"command": BARE, "exit": 1, "seconds": 61.0},
    "seeded_files": {SHOPLAB_KEY: "absent", STREAMPULSE_KEY: "absent"},
    "command": {"command": PAGE_COMMAND, "exit": 0, "seconds": 38.2},
    "profile": "core",
}


def _record(execute_module, tmp_path, relaunch):
    from docs_runner import log

    journey = _load()
    step = next(step for step in journey.steps if step.id == "recovery")
    runner = execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )
    runner.tokens[("http://localhost:28100", "admin")] = "a token of the first start"
    running = Running(
        name="compose-demo",
        base_url="http://localhost:23100/",
        api_url="http://localhost:28100",
        relaunch=relaunch,
    )
    record = runner._step(
        journey, "compose-demo", 7, step, running, None, None, None, {}, False
    )
    return runner, record


def test_the_runner_writes_the_relaunch_and_drops_the_sign_ins(
    execute_module, tmp_path
):
    asked = []
    runner, record = _record(
        execute_module, tmp_path, lambda how: asked.append(how) or dict(DONE)
    )
    assert asked == ["recovery"]
    assert record.result == "PASS"
    assert record.observed == (
        "the start without the seeds exited 1 after 61.0 s, the apps' key files"
        " absent; the page's command then exited 0 after 38.2 s, with no down -v"
        " between"
    )
    assert runner.tokens == {}
    assert record.expected_snapshot == "compose-demo/07-recovery.expected.json"
    assert json.loads((tmp_path / record.snapshot).read_text()) == DONE


def test_the_runner_fails_the_step_on_the_relaunchs_problem(execute_module, tmp_path):
    from docs_runner import report

    problem = (
        f"after the start without the seeds, {SHOPLAB_KEY} present: the recovery"
        " would recover nothing"
    )
    _, record = _record(
        execute_module, tmp_path, lambda how: {**DONE, "problem": problem}
    )
    assert record.result == "FAIL"
    assert record.observed == problem
    expected, observed = report._sides(record, tmp_path)
    assert expected.texts and observed.texts


def test_a_stack_that_cannot_relaunch_fails_the_step(execute_module, tmp_path):
    _, record = _record(execute_module, tmp_path, None)
    assert record.result == "FAIL"
    assert record.observed == "the compose-demo stack cannot be relaunched"
    assert (tmp_path / record.snapshot).is_file()
