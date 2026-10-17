"""The compose-demo journey: the compose file's demo profile, started as its page says (D73).

``tests/acceptance/docs/journeys/compose-demo.yaml`` walks
``docs/getting-started/docker-compose-file.md`` on ``compose-demo``
(``docs_runner/stacks.py``, ``ComposeDemo``): the stack started with the demo
command the page's "Services" section gives, read from the page at run time.
Its first step is a ``services`` step (``docs_runner/services.py``): both
traffic simulators keep running, unrestarted, for 60 s, and the events table
grows meanwhile. What decides whether that walk can pass for the wrong reason
is held here, in the unit job, with a fake ``docker`` on ``PATH``:

* the stack runs the page's one demo command, as written, and refuses a page
  with none, two, or one over several lines; before it, it removes the images
  of its own tag and refuses to start when one is left, or when the compose
  file names none, so the command builds them;
* its project, ports and image tag are its own: no port of a developer's
  stack (5432, 5433, 6379, 8000) and none of compose-dev's, so neither brings
  the other down; ``CORS_ORIGINS`` is the one deviation, the moved origins of
  the dashboard, ShopLab and StreamPulse;
* a ``services`` step is refused on every stack but compose-demo (compose-dev
  and demo-local named), with an ``expect`` or a ``snapshot``, and naming a
  service the compose file does not define or the guide does not name;
* the watch fails a simulator that is not running at the start (the seed has
  not run: its last line says so), one that restarts or stops in the window,
  and an events count that does not grow; and the runner writes what it saw
  and fails the step.

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
from docs_runner.model import Services, Step
from docs_runner.stacks import ComposeDemo, ComposeDev, Running, StackError

JOURNEY = RUNNER_ROOT / "journeys" / "compose-demo.yaml"
PAGE = "getting-started/docker-compose-file.md"
SIMULATORS = ["shoplab-simulator", "streampulse-simulator"]
#: The host ports a developer's own stack holds (the founder's included).
DEVELOPER_PORTS = {5432, 5433, 6379, 8000}


def _load(path: Path = JOURNEY):
    return loader.load(path, loader.context_for(REPO_ROOT))


def _journey_data() -> Dict[str, Any]:
    return yaml.safe_load(JOURNEY.read_text(encoding="utf-8"))


def _planted(tmp_path: Path, data: Dict[str, Any], name: str = "compose-demo") -> Path:
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    return path


def _refusals(path: Path) -> List[str]:
    with pytest.raises(loader.Refused) as refused:
        _load(path)
    return refused.value.problems


# ---------------------------------------------------------------------------
# The journey and its page
# ---------------------------------------------------------------------------
def test_the_journey_walks_the_page_on_compose_demo():
    journey = _load()
    assert journey.guide == PAGE
    assert journey.stack == "compose-demo"
    entry = loader.read_inventory(RUNNER_ROOT / "inventory.toml")["pages"][PAGE]
    assert entry["class"] == "journey" and entry["journey"] == "compose-demo"
    assert "pending" not in entry


def test_the_first_step_holds_both_simulators_for_a_minute_and_the_events_growing():
    """First, so a stack whose seeds have not run fails here, on the
    simulators, before any page is opened."""
    first = _load().steps[0]
    assert first.do.kind == "services"
    assert first.do.services.running == SIMULATORS
    assert first.do.services.for_seconds == 60
    assert first.do.services.growing == "events"


def test_the_journey_opens_both_apps_at_the_addresses_the_page_gives():
    journey = _load()
    opened = [step.do.open for step in journey.steps if step.do.open is not None]
    assert opened == ["http://localhost:3200", "http://localhost:3300"]
    page = guides.load(REPO_ROOT / "docs", PAGE)
    assert page.contains(
        "ShopLab then answers at http://localhost:3200 and StreamPulse at"
        " http://localhost:3300."
    )


def test_the_journey_states_its_one_deviation():
    header = JOURNEY.read_text(encoding="utf-8").split("\nguide:", 1)[0]
    assert "CORS_ORIGINS" in header and "deviation" in header


# ---------------------------------------------------------------------------
# A services step: where it may be, and what it may name
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("stack", ["compose-dev", "demo-local"])
def test_a_services_step_is_refused_on_any_other_stack(tmp_path, stack):
    data = _journey_data()
    data["stack"] = stack
    problems = _refusals(_planted(tmp_path, data))
    assert any(
        "step 1 (simulators): a services step watches the compose services" in p
        and f"{stack} is not that stack" in p
        for p in problems
    ), problems


def test_a_services_step_in_a_compose_dev_journey_is_refused(tmp_path):
    """C6 (vi): the docker-guide journey (compose-dev) given the step."""
    data = yaml.safe_load(
        (RUNNER_ROOT / "journeys" / "docker-guide.yaml").read_text(encoding="utf-8")
    )
    assert data["stack"] == "compose-dev"
    data["steps"].insert(
        0,
        {
            "id": "simulators",
            "doc": data["steps"][0]["doc"],
            "do": {"services": {"running": ["api"], "for_seconds": 60}},
            "fail": "the api does not keep running",
        },
    )
    problems = _refusals(_planted(tmp_path, data, "docker-guide"))
    assert any(
        "a services step watches the compose services the page's demo command"
        " starts (compose-demo); compose-dev is not that stack" in p
        for p in problems
    ), problems


@pytest.mark.parametrize(
    "extra, refused",
    [
        pytest.param(
            {"expect": {"status": 200}},
            "services checks what the action says",
            id="an expect",
        ),
        pytest.param(
            {"snapshot": True},
            "a services step has no screen to snapshot",
            id="a snapshot",
        ),
    ],
)
def test_a_services_step_checks_only_what_its_action_says(tmp_path, extra, refused):
    data = _journey_data()
    data["steps"][0].update(extra)
    problems = _refusals(_planted(tmp_path, data))
    assert any(refused in p for p in problems), problems


def test_a_service_the_compose_file_does_not_define_is_refused(tmp_path):
    data = _journey_data()
    data["steps"][0]["do"]["services"]["running"] = ["shoplab-traffic"]
    problems = _refusals(_planted(tmp_path, data))
    assert any(
        "'shoplab-traffic' is not a service of docker-compose.yml" in p
        for p in problems
    ), problems


def test_a_service_the_guide_does_not_name_is_refused():
    guide = guides.parse("page.md", "# Page\n\n## Services\n\nOnly `api` here.\n")
    context = loader.context_for(REPO_ROOT)
    problems = loader._services_problems(
        "step 1 (x)", ["api", "shoplab-simulator"], guide, context
    )
    assert problems == [
        "step 1 (x): services: 'shoplab-simulator' is not in the guide's text;"
        " watch the services the guide names"
    ]


def test_every_simulator_is_a_demo_profile_service_of_the_compose_file():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text())
    for name in SIMULATORS:
        assert compose["services"][name]["profiles"] == ["demo"]
        # The policy that makes a simulator that exits at start a restart.
        assert compose["services"][name]["restart"] == "unless-stopped"


@pytest.mark.parametrize(
    "plan, message",
    [
        ({"running": ["a"], "for_seconds": 9}, "greater than or equal to 10"),
        ({"running": ["a"], "for_seconds": 601}, "less than or equal to 600"),
        ({"running": ["a", "a"], "for_seconds": 60}, "service 'a' is named twice"),
        ({"running": [], "for_seconds": 60}, "at least 1 item"),
        (
            {"running": ["a"], "for_seconds": 60, "growing": "assignments"},
            "Input should be 'events'",
        ),
        ({"running": ["A b"], "for_seconds": 60}, "should match pattern"),
    ],
)
def test_the_model_bounds_a_services_step(plan, message):
    with pytest.raises(ValidationError) as caught:
        Services.model_validate(plan)
    assert message in str(caught.value)


def test_the_step_says_what_it_does_and_what_it_holds():
    step = Step.model_validate(
        {
            "id": "s",
            "doc": "services",
            "do": {
                "services": {
                    "running": SIMULATORS,
                    "for_seconds": 60,
                    "growing": "events",
                }
            },
            "fail": "x",
        }
    )
    assert describe_action(step) == (
        "watch the compose services shoplab-simulator, streampulse-simulator for"
        " 60 s, counting the events"
    )
    assert describe_expect(step) == (
        "each has one container, running at the start and through the 60 s, its"
        " restart count unchanged; the events are more at the end than at the start"
    )


# ---------------------------------------------------------------------------
# The stack: the page's command, its own project, ports and images
# ---------------------------------------------------------------------------
def test_the_stack_runs_the_one_demo_command_the_page_gives():
    command = stacks.demo_command(REPO_ROOT)
    assert command == (
        "SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait"
    )
    section = guides.load(REPO_ROOT / "docs", PAGE).section(stacks.DEMO_COMPOSE_SECTION)
    assert "```{.bash" in section and f"\n{command}\n" in section


def _page_tree(tmp_path: Path, services_section: str) -> Path:
    root = tmp_path / "planted"
    (root / "docs" / "getting-started").mkdir(parents=True)
    (root / "docs" / PAGE).write_text(
        "# The Docker Compose stack\n\n## Services\n\n"
        + services_section
        + "\n## First start\n\n```bash\ndocker compose --profile demo up -d\n```\n",
        encoding="utf-8",
    )
    return root


DEMO_BLOCK = "```bash\nSEED=demo docker compose --profile demo up -d --wait\n```\n"


@pytest.mark.parametrize(
    "section, refused",
    [
        pytest.param(
            "```bash\ndocker compose up -d --wait\n```\n",
            "gives 0 shell blocks that start the demo profile",
            id="none (one outside the section does not count)",
        ),
        pytest.param(DEMO_BLOCK + "\n" + DEMO_BLOCK, "gives 2 shell blocks", id="two"),
        pytest.param(
            "```bash\nexport SEED=demo\ndocker compose --profile demo up -d\n```\n",
            "demo command is not one line",
            id="over two lines",
        ),
    ],
)
def test_a_page_without_exactly_one_demo_command_is_refused(tmp_path, section, refused):
    with pytest.raises(StackError, match=refused):
        stacks.demo_command(_page_tree(tmp_path, section))


def test_the_stack_differs_from_compose_dev_by_project_ports_tag_and_cors(tmp_path):
    environ = {"PATH": "/usr/bin"}
    dev = ComposeDev(tmp_path, tmp_path / "run", environ).env("core")
    demo_stack = ComposeDemo(tmp_path, tmp_path / "run", environ)
    demo = demo_stack.env("core")
    assert set(demo) - set(dev) == {
        "CORS_ORIGINS",
        "SHOPLAB_HOST_PORT",
        "STREAMPULSE_HOST_PORT",
    }
    assert set(dev) - set(demo) == set()
    changed = {key for key in dev if dev[key] != demo[key]}
    host_ports = {key for key in demo if key.endswith("_HOST_PORT")}
    # The project, the image tag (whose value names the project) and the ports.
    assert changed - host_ports == {
        key for key in dev if dev[key] in ("docs-journeys", "docs-journeys-core")
    }
    assert demo["COMPOSE_PROJECT_NAME"] == "docs-journeys-demo"
    assert demo_stack.image_tag("core") == "docs-journeys-demo-core"
    assert demo_stack.image_tag("core") in demo.values()
    assert demo["CORS_ORIGINS"] == (
        "http://localhost:23100,http://localhost:23200,http://localhost:23300"
    )
    ports = {int(demo[key]) for key in host_ports}
    assert len(ports) == 6
    assert not ports & DEVELOPER_PORTS
    assert not ports & {int(dev[key]) for key in dev if key.endswith("_HOST_PORT")}
    assert not ports & {3000, 3100, 3200, 3300}


FAKE_DOCKER = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
args = sys.argv[1:]
with open(os.path.join(here, "calls.log"), "a") as log:
    log.write(" ".join(args) + " | SEED=" + os.environ.get("SEED", "unset")
              + " | PROJECT=" + os.environ.get("COMPOSE_PROJECT_NAME", "unset") + "\\n")
path = os.path.join(here, "state.json")
with open(path) as handle:
    state = json.load(handle)


def save():
    with open(path, "w") as handle:
        json.dump(state, handle)


def pop(name):
    values = state[name]
    value = values.pop(0) if len(values) > 1 else values[0]
    save()
    return value


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
        sys.exit(state.get("config_exit", 0))
    if command == "up":
        sys.exit(state.get("up_exit", 0))
    if command == "ps":
        print("\\n".join(state.get("containers", {{}}).get(words[-1], [])))
        sys.exit(state.get("ps_exit", 0))
    if command == "exec":
        print(pop("counts"))
        sys.exit(0)
    if command == "logs":
        print(state.get("logs", {{}}).get(words[-1], ""))
    sys.exit(0)
if args[:2] == ["image", "rm"]:
    state.setdefault("removed", []).append(args[-1])
    save()
    sys.exit(0)
if args[:2] == ["image", "inspect"]:
    gone = args[-1] in state.get("removed", []) and not state.get("rm_keeps")
    sys.exit(1 if gone else 0)
if args[:1] == ["inspect"]:
    print(pop("inspect:" + args[-1]))
    sys.exit(0)
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
OWN_IMAGES = ["a-api:docs-journeys-demo-core", "a-shoplab:docs-journeys-demo-core"]
LISTED = [
    "postgres:16-alpine",
    OWN_IMAGES[1],
    "a-api:docs-journeys-core",
    OWN_IMAGES[0],
    "python:3.11-slim",
]
DEMO = "compose -p docs-journeys-demo --profile demo"
PAGE_COMMAND = "compose --profile demo up -d --wait"


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
    _state(root, {"images": LISTED})
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


def _stack(cls, tree: Path, monkeypatch):
    stack = cls(tree, tree.parent / "run" / "stacks", _environ(tree))
    monkeypatch.setattr(stack, "_served_profile", lambda api_url: "core")
    return stack


def _project(call: str) -> str:
    return f"{call} | SEED=unset | PROJECT=docs-journeys-demo"


def _removal(images: List[str]) -> List[str]:
    return [_project(f"image rm --force {image}") for image in images] + [
        _project(f"image inspect --format {{{{.Id}}}} {image}") for image in images
    ]


def test_up_removes_its_own_images_then_runs_the_page_command_as_written(
    tree, monkeypatch
):
    running = _stack(ComposeDemo, tree, monkeypatch).up("core")
    assert _calls(tree) == [
        _project(f"{DEMO} down -v --remove-orphans"),
        _project(f"{DEMO} config --images"),
        *_removal(OWN_IMAGES),
        # The page's own command, with the seeds it sets and no --build.
        f"{PAGE_COMMAND} | SEED=demo,shoplab,streampulse | PROJECT=docs-journeys-demo",
    ]
    assert running.name == "compose-demo"
    assert running.base_url == "http://localhost:23100/"
    assert running.api_url == "http://localhost:28100"
    assert dict(running.published) == {
        3000: "http://localhost:23100",
        8000: "http://localhost:28100",
        3200: "http://localhost:23200",
        3300: "http://localhost:23300",
    }
    assert running.compose.compose == [
        "docker",
        "compose",
        "-p",
        "docs-journeys-demo",
        "--profile",
        "demo",
    ]
    assert dict(running.accounts) == stacks.DEMO_ACCOUNTS


def test_an_image_left_after_its_removal_stops_the_stack_before_the_command(
    tree, monkeypatch
):
    _state(tree, {"images": LISTED, "rm_keeps": True})
    with pytest.raises(StackError, match="are still there after docker image rm"):
        _stack(ComposeDemo, tree, monkeypatch).up("core")
    calls = _calls(tree)
    assert not [call for call in calls if call.startswith(PAGE_COMMAND)]
    assert calls[-1] == _project(f"{DEMO} down -v --remove-orphans")


def test_a_compose_file_naming_no_image_of_its_tag_is_refused(tree, monkeypatch):
    _state(tree, {"images": ["postgres:16-alpine", "a-api:docs-journeys-core"]})
    with pytest.raises(
        StackError, match="lists no image tagged docs-journeys-demo-core"
    ):
        _stack(ComposeDemo, tree, monkeypatch).up("core")
    assert not [call for call in _calls(tree) if call.startswith(PAGE_COMMAND)]


def test_a_failed_page_command_fails_the_stack_and_clears_its_project(
    tree, monkeypatch
):
    _state(tree, {"images": LISTED, "up_exit": 1})
    stack = _stack(ComposeDemo, tree, monkeypatch)
    with pytest.raises(StackError, match=r"the page's demo command \(SEED=demo"):
        stack.up("core")
    assert stack.running is None
    assert _calls(tree)[-1] == _project(f"{DEMO} down -v --remove-orphans")


def test_compose_demo_and_compose_dev_do_not_bring_each_other_down(tree, monkeypatch):
    dev = _stack(ComposeDev, tree, monkeypatch)
    demo = _stack(ComposeDemo, tree, monkeypatch)
    dev.up("core")
    demo.up("core")
    assert dev.running is not None and demo.running is not None
    # After compose-dev's start, nothing touched its project.
    after = _calls(tree)[2:]
    assert not [call for call in after if "-p docs-journeys " in call]


def test_down_clears_the_demo_profile_and_removes_its_images(tree, monkeypatch):
    stack = _stack(ComposeDemo, tree, monkeypatch)
    stack.up("core")
    _state(tree, {"images": LISTED})
    before = len(_calls(tree))
    stack.down()
    assert stack.running is None
    assert _calls(tree)[before:] == [
        _project(f"{DEMO} down -v --remove-orphans"),
        _project(f"{DEMO} config --images"),
        *_removal(OWN_IMAGES),
    ]


def test_the_page_command_is_bounded_well_inside_the_jobs_hour(tree, monkeypatch):
    """compose-demo sorts first in a job of 60 minutes (about 15 on main): a
    hung build must fail this journey, not the job and every verdict in it."""
    stack = _stack(ComposeDemo, tree, monkeypatch)
    assert stack.timeout == stacks.DEMO_COMPOSE_TIMEOUT == 1500
    bounds = []
    real = stacks._run_logged

    def spy(argv, **kwargs):
        if argv[:2] == ["sh", "-c"]:
            bounds.append(kwargs["timeout"])
        return real(argv, **kwargs)

    monkeypatch.setattr(stacks, "_run_logged", spy)
    stack.up("core")
    assert bounds == [1500.0]
    moved = ComposeDemo(
        tree,
        tree.parent / "run",
        {**_environ(tree), "DOCS_JOURNEY_DEMO_COMPOSE_TIMEOUT": "900"},
    )
    assert moved.timeout == 900.0


# ---------------------------------------------------------------------------
# The watch (services.py), against the fake docker
# ---------------------------------------------------------------------------
class _Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


SEED_MISSING = (
    "no /data/demo/shoplab/.api_key: the shoplab seed has not run. Start the stack"
    " with: SEED=demo,shoplab,streampulse docker compose --profile demo up -d --wait"
)


def _watch(tree: Path, state: Dict[str, Any], growing="events"):
    base = {
        "containers": {"shoplab-simulator": ["c1"], "streampulse-simulator": ["c2"]},
        "inspect:c1": ['"running" 0'],
        "inspect:c2": ['"running" 2'],
        "counts": ["100", "250"],
    }
    base.update(state)
    _state(tree, base)
    compose = services.ComposeServices(
        ["docker", "compose", "-p", "p", "--profile", "demo"],
        tree,
        _environ(tree),
        tree.parent / "run" / "stacks" / "compose-demo.log",
    )
    clock = _Clock()
    return services.watch(
        compose, SIMULATORS, 60, growing, clock=clock, sleep=clock.sleep
    )


def test_running_unrestarted_simulators_and_growing_events_pass(tree):
    watched = _watch(tree, {})
    assert watched.problems == []
    assert watched.counts == [100, 250]
    assert watched.seconds == 60
    assert watched.summary() == (
        "shoplab-simulator, streampulse-simulator running for 60 s, restarts"
        " unchanged (shoplab-simulator 0, streampulse-simulator 2); events 100 -> 250"
    )
    # A restart count the stack had before the window is not held against it.
    assert watched.record()["services"]["streampulse-simulator"]["end"] == {
        "containers": 1,
        "status": "running",
        "restarts": 2,
    }


def test_a_simulator_whose_seed_has_not_run_fails_with_its_last_line(tree):
    """C6 (i), offline: the bare command applies only the demo seed."""
    watched = _watch(
        tree,
        {
            "inspect:c1": ['"restarting" 3'],
            "logs": {"shoplab-simulator": "starting\n" + SEED_MISSING + "\n"},
        },
    )
    assert watched.problems[0] == "shoplab-simulator is restarting, not running"
    assert watched.problems[-1] == (
        "shoplab-simulator last printed: " + SEED_MISSING[:200]
    )
    assert "the shoplab seed has not run" in watched.problems[-1]
    assert watched.counts == []
    assert SEED_MISSING in watched.logs["shoplab-simulator"]


def test_a_simulator_that_restarts_in_the_window_fails(tree):
    """C6 (iii), offline: a simulator stopped mid-window is restarted."""
    watched = _watch(
        tree, {"inspect:c2": ['"running" 2', '"running" 2', '"running" 3']}
    )
    assert watched.problems[0] == (
        "streampulse-simulator restarted: RestartCount 2 -> 3 after 10 s"
    )
    assert watched.counts == [100]


def test_a_simulator_that_stops_in_the_window_fails(tree):
    watched = _watch(tree, {"inspect:c1": ['"running" 0', '"exited" 0']})
    assert watched.problems[0] == "shoplab-simulator is exited, not running after 5 s"


def test_events_that_do_not_grow_fail(tree):
    """C6 (iv), offline: the count at the end is the count at the start."""
    watched = _watch(tree, {"counts": ["100", "100"]})
    assert watched.problems == ["the events did not grow in 60 s: 100 -> 100"]


def test_a_service_without_exactly_one_container_fails(tree):
    watched = _watch(tree, {"containers": {"shoplab-simulator": ["c1", "c3"]}})
    assert watched.problems[:2] == [
        "shoplab-simulator has 2 containers, not one",
        "streampulse-simulator has 0 containers, not one",
    ]


@pytest.mark.parametrize(
    "state, problem",
    [
        ({"ps_exit": 1}, "docker compose ps shoplab-simulator exited 1"),
        ({"counts": ["ERROR: no such table"]}, "counting the events exited 0"),
    ],
)
def test_docker_that_does_not_answer_fails_the_watch(tree, state, problem):
    watched = _watch(tree, state)
    assert watched.problems and watched.problems[0].startswith(problem)


def test_without_growing_nothing_is_counted(tree):
    watched = _watch(tree, {"counts": ["not a number"]}, growing=None)
    assert watched.problems == [] and watched.counts == []
    assert not [call for call in _calls(tree) if " exec " in f" {call} "]


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


class _Compose:
    """A stand-in for ComposeServices: each service running, with the restart
    counts given for it (0 throughout by default), and the counts given."""

    def __init__(self, restarts: Dict[str, List[int]], counts: List[int]):
        self.restarts = restarts
        self.counts = counts

    def state(self, service: str) -> services.State:
        seen = self.restarts.setdefault(service, [0])
        value = seen.pop(0) if len(seen) > 1 else seen[0]
        return services.State(service, 1, "running", value)

    def count(self, what: str) -> int:
        return self.counts.pop(0)

    def logs(self, service: str, lines: int = 20) -> str:
        return "a line\n"


def _record(execute_module, monkeypatch, tmp_path, compose):
    from docs_runner import log

    clock = _Clock()
    real = services.watch
    monkeypatch.setattr(
        execute_module.compose_services,
        "watch",
        lambda *args, **kwargs: real(*args, clock=clock, sleep=clock.sleep, **kwargs),
    )
    journey = _load()
    runner = execute_module.JourneyRunner(
        None,
        None,
        log.Log(tmp_path / "results.jsonl"),
        execute_module.Settings(run_dir=tmp_path, run_id="r", sha="s"),
    )
    running = Running(
        name="compose-demo",
        base_url="http://localhost:23100/",
        api_url="http://localhost:28100",
        compose=compose,
    )
    return runner._step(
        journey,
        "compose-demo",
        1,
        journey.steps[0],
        running,
        None,
        None,
        None,
        {},
        False,
    )


def test_the_runner_writes_what_the_watch_saw_and_passes_the_step(
    execute_module, monkeypatch, tmp_path
):
    record = _record(execute_module, monkeypatch, tmp_path, _Compose({}, [100, 250]))
    assert record.result == "PASS"
    assert record.observed.endswith("events 100 -> 250")
    assert record.expected_snapshot == "compose-demo/01-simulators.expected.json"
    expected = json.loads((tmp_path / record.expected_snapshot).read_text())
    assert expected["running"] == SIMULATORS and expected["growing"] == "events"
    seen = json.loads((tmp_path / record.snapshot).read_text())
    assert record.snapshot == "compose-demo/01-simulators.services.json"
    assert seen["counts"] == [100, 250] and seen["problems"] == []


def test_the_runner_fails_the_step_on_the_watchs_first_problem(
    execute_module, monkeypatch, tmp_path
):
    from docs_runner import report

    record = _record(
        execute_module,
        monkeypatch,
        tmp_path,
        _Compose({"shoplab-simulator": [0, 0, 1]}, [100]),
    )
    assert record.result == "FAIL"
    assert record.observed == (
        "shoplab-simulator restarted: RestartCount 0 -> 1 after 10 s;"
        " shoplab-simulator last printed: a line"
    )
    seen = json.loads((tmp_path / record.snapshot).read_text())
    assert seen["problems"][0].startswith("shoplab-simulator restarted")
    # The report can show a FAIL of this step: both of its files are there.
    expected, observed = report._sides(record, tmp_path)
    assert expected.texts and observed.texts


def test_a_stack_without_compose_services_fails_the_step(
    execute_module, monkeypatch, tmp_path
):
    record = _record(execute_module, monkeypatch, tmp_path, None)
    assert record.result == "FAIL"
    assert record.observed == "the compose-demo stack has no compose services to watch"
    assert (tmp_path / record.snapshot).is_file()
