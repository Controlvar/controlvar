"""The demo-local stack and the demo journeys' loading (#1142).

``demo-local`` runs ``./demo/setup-local.sh`` and ``./demo/teardown-local.sh``
(``tests/acceptance/docs/docs_runner/stacks.py``). The script binds fixed host
ports, so the stack never starts it off a CI runner, and the real script is
never run here: every test below gives the stack a temporary checkout holding a
fake script, and fake addresses served on free ports. What is checked:

* the refusals before the script runs: off a CI runner, without the demo job's
  switch, and with one of its ports already answering;
* a start: the "Ready!" box's addresses become the stack's, every one must
  answer, and the administrator's password and the API keys the seeds wrote
  are kept out of the stack's log and handed to the runner and the scan;
* a failed start: every later journey fails the same way, and the script is
  not run again over what it left;
* the stop: the guide's command must exit 0, leave no port answering and
  remove what the guide says it removes;
* the parsers against the real script: its box and its password export;
* every key file the seeds setup-local.sh runs and the script itself write
  (the seeds' ``*KEY_FILE`` constants, read with ``ast``) is in the list the
  stack reads, and a key file added to a seed is found;
* a journey whose guide is a document (``[documents]``), and the session's
  choice of stacks (``DOCS_JOURNEY_STACKS``);
* the end-of-run scan looks for the values a stack keeps, not only the
  runner's.

Temporary files and loopback sockets only; no git, no network, no docker.
"""

from __future__ import annotations

import ast
import copy
import datetime
import http.server
import importlib.util
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any, Dict, Iterator, List, Set, Tuple

import pytest
import yaml

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
RUNNER_ROOT = REPO_ROOT / "tests" / "acceptance" / "docs"
if str(RUNNER_ROOT) not in sys.path:
    sys.path.insert(0, str(RUNNER_ROOT))

from docs_runner import loader, stacks
from docs_runner.stacks import DemoLocal, StackError

ON_CI = {"GITHUB_ACTIONS": "true", "DOCS_JOURNEY_DEMO_LOCAL": "1"}
PASSWORD = "Demo1234!"
KEYS = {
    "demo/.demo_api_key": "eptk_demo_simulator_key_0123456789",
    "demo/shoplab/.api_key": "eptk_shoplab_storefront_key_0123456789",
    "demo/streampulse/.api_key": "eptk_streampulse_app_key_0123456789",
    "demo/streampulse/.api_key_server": "eptk_streampulse_second_key_0123456789",
}


# ---------------------------------------------------------------------------
# A temporary checkout with fake start and stop commands
# ---------------------------------------------------------------------------
class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:
        return None


@pytest.fixture
def served() -> Iterator[List[int]]:
    """Four free loopback ports, each answering HTTP: the dashboard, ShopLab,
    StreamPulse and the API."""
    servers = []
    for _ in range(4):
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
    yield [server.server_address[1] for server in servers]
    for server in servers:
        server.shutdown()
        server.server_close()


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _executable(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _box(ports: List[int]) -> str:
    dashboard, shoplab, streampulse, api = ports
    return (
        "printf '\\033[0;32mControlvar Demo — Ready!\\033[0m\\n'\n"
        f"printf '  Frontend:    http://localhost:{dashboard}\\n'\n"
        f"printf '  ShopLab:     http://localhost:{shoplab}\\n'\n"
        f"printf '  StreamPulse: http://localhost:{streampulse}\\n'\n"
        f"printf '  API docs:    http://localhost:{api}/api/v1/docs\\n'\n"
        "printf '  Admin:       admin@demo.com\\n'\n"
        f"printf '  Password:    {PASSWORD}\\n'\n"
    )


def _checkout(
    tmp_path: Path, ports: List[int], *, exit_code: int = 0, box: bool = True
) -> Path:
    """A checkout whose setup-local.sh writes the key files, logs and pids,
    prints a key and the password, and its box; teardown removes them."""
    root = tmp_path / "repo"
    keys = "".join(
        f"mkdir -p \"$(dirname '{path}')\"\nprintf '%s\\n' '{value}' > '{path}'\n"
        for path, value in KEYS.items()
    )
    _executable(
        root / "demo" / "setup-local.sh",
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        'cd "$(dirname "$0")/.."\n'
        "printf 'run\\n' >> runs.log\n"
        "mkdir -p demo/.logs demo/.pids\n"
        f"{keys}"
        f"printf 'backend started with {KEYS['demo/.demo_api_key']}\\n' > demo/.logs/backend.log\n"
        "printf 'seeded\\n' > demo/.logs/simulator.log\n"
        "printf '1\\n' > demo/.pids/backend.pid\n"
        + "".join(f"printf 'key {value}\\n'\n" for value in KEYS.values())
        + "export FIRST_SUPERUSER_PASSWORD='Demo1234!'\n"
        + (_box(ports) if box else "printf 'done\\n'\n")
        + f"exit {exit_code}\n",
    )
    _executable(
        root / "demo" / "teardown-local.sh",
        "#!/usr/bin/env bash\n"
        'cd "$(dirname "$0")/.."\n'
        "rm -rf demo/.pids demo/.logs\n"
        "rm -f demo/.demo_api_key\n"
        "printf 'stopped\\n'\n",
    )
    return root


@pytest.fixture
def quiet_ports(monkeypatch) -> List[int]:
    """DEMO_PORTS as ports nothing listens on, so the busy check passes."""
    ports = [_free_port() for _ in range(3)]
    monkeypatch.setattr(stacks, "DEMO_PORTS", tuple(ports))
    return ports


def _stack(root: Path, tmp_path: Path, monkeypatch, **environ: str) -> DemoLocal:
    monkeypatch.setattr(
        stacks.ComposeDev, "_served_profile", staticmethod(lambda url: "full")
    )
    env = {"PATH": os.environ["PATH"], **ON_CI, **environ}
    stack = DemoLocal(root, tmp_path / "run" / "stacks", env)
    stack.STOP_WAIT = 0.5
    return stack


# ---------------------------------------------------------------------------
# Refusals before the script runs
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "environ, missing",
    [
        ({}, "GITHUB_ACTIONS=true and DOCS_JOURNEY_DEMO_LOCAL=1"),
        ({"GITHUB_ACTIONS": "true"}, "DOCS_JOURNEY_DEMO_LOCAL=1"),
        ({"DOCS_JOURNEY_DEMO_LOCAL": "1"}, "GITHUB_ACTIONS=true"),
        (
            {"GITHUB_ACTIONS": "false", "DOCS_JOURNEY_DEMO_LOCAL": "1"},
            "GITHUB_ACTIONS=true",
        ),
    ],
    ids=["laptop", "ci-without-the-switch", "switch-off-ci", "actions-false"],
)
def test_the_stack_refuses_to_start_off_the_demo_job(
    tmp_path, served, quiet_ports, environ, missing
):
    root = _checkout(tmp_path, served)
    stack = DemoLocal(
        root, tmp_path / "run" / "stacks", {"PATH": os.environ["PATH"], **environ}
    )
    with pytest.raises(StackError, match=f"runs only on a CI runner.*lacks {missing}"):
        stack.up("full")
    assert not (root / "runs.log").exists(), "the script ran"
    assert not (root / "demo" / ".logs").exists()


def test_the_stack_refuses_to_start_over_a_port_that_answers(
    tmp_path, served, monkeypatch
):
    monkeypatch.setattr(stacks, "DEMO_PORTS", (served[0],))
    root = _checkout(tmp_path, served)
    stack = _stack(root, tmp_path, monkeypatch)
    with pytest.raises(StackError, match=f"port {served[0]} already answers"):
        stack.up("full")
    assert not (root / "runs.log").exists(), "the script ran"


# ---------------------------------------------------------------------------
# A start
# ---------------------------------------------------------------------------
def test_a_start_takes_its_addresses_from_the_ready_box(
    tmp_path, served, quiet_ports, monkeypatch
):
    root = _checkout(tmp_path, served)
    stack = _stack(root, tmp_path, monkeypatch)
    running = stack.up("full")
    dashboard, shoplab, streampulse, api = served
    assert running.name == "demo-local"
    assert running.base_url == f"http://localhost:{dashboard}/"
    assert running.api_url == f"http://localhost:{api}"
    assert running.published == {port: f"http://localhost:{port}" for port in served}
    assert running.profile == "full"
    assert running.accounts == stacks.DEMO_ACCOUNTS
    assert running.up_seconds > 0
    # The same Running for the next journey; the script ran once.
    assert stack.up("full") is running
    assert (root / "runs.log").read_text().splitlines() == ["run"]


def test_the_password_and_every_key_are_kept_out_of_the_log_and_handed_on(
    tmp_path, served, quiet_ports, monkeypatch
):
    root = _checkout(tmp_path, served)
    stack = _stack(root, tmp_path, monkeypatch)
    running = stack.up("full")
    assert set(running.secrets) == {PASSWORD, *KEYS.values()}
    log = (tmp_path / "run" / "stacks" / "demo-local.log").read_text()
    assert "Ready!" in log and "(redacted)" in log
    for value in (PASSWORD, *KEYS.values()):
        assert value not in log
    assert "./demo/setup-local.sh exited 0 after" in log


def test_the_stack_fails_when_the_profile_is_not_the_journeys(
    tmp_path, served, quiet_ports, monkeypatch
):
    root = _checkout(tmp_path, served)
    stack = _stack(root, tmp_path, monkeypatch)
    with pytest.raises(StackError, match="serves the full profile, not core"):
        stack.up("core")


def test_a_box_address_that_never_answers_fails_the_start(
    tmp_path, served, quiet_ports, monkeypatch
):
    ports = [served[0], _free_port(), served[2], served[3]]
    root = _checkout(tmp_path, ports)
    stack = _stack(root, tmp_path, monkeypatch, DOCS_JOURNEY_DEMO_WAIT="1")
    with pytest.raises(StackError, match=f"localhost:{ports[1]}/ did not answer"):
        stack.up("full")


# ---------------------------------------------------------------------------
# A failed start
# ---------------------------------------------------------------------------
def test_a_failed_script_fails_every_journey_and_is_not_run_again(
    tmp_path, served, quiet_ports, monkeypatch
):
    root = _checkout(tmp_path, served, exit_code=3)
    stack = _stack(root, tmp_path, monkeypatch)
    with pytest.raises(StackError, match=r"setup-local.sh exited 3 after \d+ s"):
        stack.up("full")
    with pytest.raises(StackError, match=r"setup-local.sh exited 3"):
        stack.up("full")
    assert (root / "runs.log").read_text().splitlines() == ["run"]
    # What it left is known, so the scan looks for it, and the logs it wrote
    # are kept with the values taken out.
    assert set(stack.secrets) == {PASSWORD, *KEYS.values()}
    copied = tmp_path / "run" / "stacks" / "demo-local" / "backend.log"
    assert copied.read_text() == "backend started with (redacted)\n"


def test_a_script_that_prints_no_ready_box_fails_the_start(
    tmp_path, served, quiet_ports, monkeypatch
):
    root = _checkout(tmp_path, served, box=False)
    stack = _stack(root, tmp_path, monkeypatch)
    with pytest.raises(StackError, match="without its 'Ready!' box"):
        stack.up("full")


# ---------------------------------------------------------------------------
# The stop
# ---------------------------------------------------------------------------
def test_the_guides_stop_command_runs_and_is_checked(
    tmp_path, served, quiet_ports, monkeypatch
):
    root = _checkout(tmp_path, served)
    stack = _stack(root, tmp_path, monkeypatch)
    stack.up("full")
    stack.down()
    log = (tmp_path / "run" / "stacks" / "demo-local.log").read_text()
    assert "$ ./demo/teardown-local.sh\nstopped\n" in log
    # Each process's log was kept before the stop removed demo/.logs.
    kept = tmp_path / "run" / "stacks" / "demo-local"
    assert sorted(path.name for path in kept.iterdir()) == [
        "backend.log",
        "simulator.log",
    ]
    assert stack.started is False
    stack.down()  # a second stop runs nothing


def test_a_stop_that_leaves_the_logs_fails(tmp_path, served, quiet_ports, monkeypatch):
    root = _checkout(tmp_path, served)
    _executable(root / "demo" / "teardown-local.sh", "#!/usr/bin/env bash\nexit 0\n")
    stack = _stack(root, tmp_path, monkeypatch)
    stack.up("full")
    with pytest.raises(
        StackError, match="left: demo/.pids, demo/.logs, demo/.demo_api_key still there"
    ):
        stack.down()


def test_a_stop_that_leaves_a_port_answering_fails(
    tmp_path, served, quiet_ports, monkeypatch
):
    root = _checkout(tmp_path, served)
    stack = _stack(root, tmp_path, monkeypatch)
    stack.up("full")
    monkeypatch.setattr(stacks, "DEMO_PORTS", (served[1],))
    with pytest.raises(StackError, match=f"port {served[1]} still answers"):
        stack.down()


def test_a_stop_that_exits_1_fails(tmp_path, served, quiet_ports, monkeypatch):
    root = _checkout(tmp_path, served)
    _executable(
        root / "demo" / "teardown-local.sh",
        "#!/usr/bin/env bash\n"
        'cd "$(dirname "$0")/.."\n'
        "rm -rf demo/.pids demo/.logs demo/.demo_api_key\nexit 1\n",
    )
    stack = _stack(root, tmp_path, monkeypatch)
    stack.up("full")
    with pytest.raises(StackError, match="teardown-local.sh exited 1"):
        stack.down()


def test_a_stack_that_never_started_stops_nothing(tmp_path, served, quiet_ports):
    root = _checkout(tmp_path, served)
    stack = DemoLocal(root, tmp_path / "run" / "stacks", {"PATH": os.environ["PATH"]})
    with pytest.raises(StackError):
        stack.up("full")
    stack.down()
    assert not (tmp_path / "run" / "stacks" / "demo-local.log").exists()


# ---------------------------------------------------------------------------
# The parsers, against the real script
# ---------------------------------------------------------------------------
def _rendered_box(script: str) -> str:
    """The script's own "Ready!" box, as it prints it with both apps started."""
    colours = {
        "${GREEN}": "\x1b[0;32m",
        "${BOLD}": "\x1b[1m",
        "${NC}": "\x1b[0m",
    }
    lines = []
    start = script.index("# 15. Success banner")
    for line in script[start:].splitlines():
        line = line.strip()
        if "║" not in line or line.count('"') < 2:
            continue
        text = line[line.index('"') + 1 : line.rindex('"')]
        for name, code in colours.items():
            text = text.replace(name, code)
        lines.append(text)
    return "\n".join(lines) + "\n"


def test_the_real_scripts_ready_box_names_the_guides_addresses():
    script = (REPO_ROOT / "demo" / "setup-local.sh").read_text(encoding="utf-8")
    addresses = stacks.ready_addresses(_rendered_box(script))
    assert addresses == {
        "Frontend": "http://localhost:3100",
        "ShopLab": "http://localhost:3200",
        "StreamPulse": "http://localhost:3300",
        "API docs": "http://localhost:8000/api/v1/docs",
    }
    guide = (REPO_ROOT / "demo" / "DEMO_GUIDE.md").read_text(encoding="utf-8")
    for port in (3100, 3200, 3300, 8000):
        assert f"http://localhost:{port}" in guide


def test_the_real_scripts_ports_are_the_stacks():
    script = (REPO_ROOT / "demo" / "setup-local.sh").read_text(encoding="utf-8")
    for port in stacks.DEMO_PORTS:
        assert str(port) in script, port


def test_the_password_the_real_script_sets_is_the_demo_accounts():
    script = (REPO_ROOT / "demo" / "setup-local.sh").read_text(encoding="utf-8")
    assert stacks.demo_password(script) == stacks.DEMO_ACCOUNTS["admin"][1]


@pytest.mark.parametrize(
    "line, value",
    [
        ("export FIRST_SUPERUSER_PASSWORD='Demo1234!'", "Demo1234!"),
        ('export FIRST_SUPERUSER_PASSWORD="An0ther pass"', "An0ther pass"),
        ("  export FIRST_SUPERUSER_PASSWORD=Plain1234", "Plain1234"),
        ("# export FIRST_SUPERUSER_PASSWORD='x'", ""),
        ("FIRST_SUPERUSER_PASSWORD='Demo1234!'", ""),
    ],
)
def test_the_password_export_is_read_as_bash_reads_it(line, value):
    assert stacks.demo_password(f"set -e\n{line}\n") == value


def test_a_box_from_before_the_last_ready_is_not_read():
    assert stacks.ready_addresses("Frontend: http://localhost:1\nno box\n") == {}


# ---------------------------------------------------------------------------
# Every key file the seeds and the script write is one the stack reads
# ---------------------------------------------------------------------------
#: A seed setup-local.sh runs: ``python backend/scripts/seed_<name>.py``.
SEED_RUN = re.compile(r"^\s*python (backend/scripts/seed_[a-z_]{1,40}\.py)\b", re.M)
#: A file setup-local.sh itself writes a key to: ``> "$DEMO_DIR/<name>"``.
SCRIPT_KEY = re.compile(r'>\s*"\$DEMO_DIR/([\w.-]{0,60}key[\w.-]{0,60})"')


def _literal_chain(node: ast.AST):
    """``NAME / "a" / "b"`` as ("NAME", ["a", "b"]), or None."""
    parts: List[str] = []
    while (
        isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.Div)
        and isinstance(node.right, ast.Constant)
        and isinstance(node.right.value, str)
    ):
        parts.insert(0, node.right.value)
        node = node.left
    if isinstance(node, ast.Name) and parts:
        return node.id, parts
    return None


def seed_key_files(source: str) -> Tuple[Set[str], Set[str]]:
    """The repository paths a seed's module-level ``*KEY_FILE`` constants name,
    and the names of those it cannot resolve.

    A constant is ``<place> / "<literal>" ...`` where the place is
    ``PROJECT_ROOT`` (the repository's root) or a constant resolved before it;
    an environment override around it (``os.environ.get(...) or ...``) is the
    default the demo uses.
    """
    places: Dict[str, str] = {"PROJECT_ROOT": ""}
    found: Set[str] = set()
    unresolved: Set[str] = set()
    for statement in ast.parse(source).body:
        if not (
            isinstance(statement, ast.Assign)
            and len(statement.targets) == 1
            and isinstance(statement.targets[0], ast.Name)
        ):
            continue
        name = statement.targets[0].id
        chains = [
            chain
            for chain in (_literal_chain(node) for node in ast.walk(statement.value))
            if chain is not None and chain[0] in places
        ]
        if not chains:
            if name.endswith("KEY_FILE"):
                unresolved.add(name)
            continue
        root, parts = max(chains, key=lambda chain: len(chain[1]))
        path = "/".join(part for part in (places[root], *parts) if part)
        places[name] = path
        if name.endswith("KEY_FILE"):
            found.add(path)
    return found, unresolved


def key_files_written(repo_root: Path) -> Tuple[Set[str], List[str], Set[str]]:
    """Every key file setup-local.sh's seeds and the script write, the seeds
    read, and the constants that could not be resolved."""
    script = (repo_root / stacks.DEMO_SCRIPT).read_text(encoding="utf-8")
    seeds = sorted(set(SEED_RUN.findall(script)))
    found = {f"demo/{name}" for name in SCRIPT_KEY.findall(script)}
    unresolved: Set[str] = set()
    for seed in seeds:
        paths, missed = seed_key_files((repo_root / seed).read_text(encoding="utf-8"))
        found |= paths
        unresolved |= {f"{seed}:{name}" for name in missed}
    return found, seeds, unresolved


def test_the_stack_keeps_every_key_file_the_seeds_and_the_script_write():
    found, seeds, unresolved = key_files_written(REPO_ROOT)
    assert seeds == [
        "backend/scripts/seed_demo_data.py",
        "backend/scripts/seed_shoplab.py",
        "backend/scripts/seed_streampulse.py",
    ]
    assert not unresolved, unresolved
    assert "demo/streampulse/.api_key_server" in found
    listed = {path.as_posix() for path in stacks.DEMO_KEY_FILES}
    assert found == listed, (
        f"written but not kept out: {sorted(found - listed)};"
        f" listed but written by nothing: {sorted(listed - found)}"
    )


def _seeds_copy(tmp_path: Path) -> Path:
    root = tmp_path / "tree"
    (root / "demo").mkdir(parents=True)
    shutil.copy(REPO_ROOT / stacks.DEMO_SCRIPT, root / stacks.DEMO_SCRIPT)
    (root / "backend" / "scripts").mkdir(parents=True)
    for seed in ("seed_demo_data.py", "seed_shoplab.py", "seed_streampulse.py"):
        shutil.copy(
            REPO_ROOT / "backend" / "scripts" / seed, root / "backend" / "scripts"
        )
    return root


def test_a_key_file_a_seed_adds_is_found(tmp_path):
    """The plant: a new ``*KEY_FILE`` constant in a seed. The list the stack
    reads no longer matches, and the difference names the new file."""
    root = _seeds_copy(tmp_path)
    seed = root / "backend" / "scripts" / "seed_streampulse.py"
    seed.write_text(
        seed.read_text(encoding="utf-8")
        + '\nEXTRA_KEY_FILE = STREAMPULSE_DIR / ".api_key_extra"\n',
        encoding="utf-8",
    )
    found, _, unresolved = key_files_written(root)
    assert not unresolved
    listed = {path.as_posix() for path in stacks.DEMO_KEY_FILES}
    assert found - listed == {"demo/streampulse/.api_key_extra"}


def test_a_key_file_the_script_adds_is_found(tmp_path):
    root = _seeds_copy(tmp_path)
    script = root / stacks.DEMO_SCRIPT
    script.write_text(
        script.read_text(encoding="utf-8")
        + 'printf \'%s\\n\' "$KEY" > "$DEMO_DIR/.another_api_key"\n',
        encoding="utf-8",
    )
    found, _, _ = key_files_written(root)
    assert "demo/.another_api_key" in found


def test_a_key_file_constant_the_reader_cannot_place_is_reported():
    _, unresolved = seed_key_files('NEW_KEY_FILE = somewhere() / ".key"\n')
    assert unresolved == {"NEW_KEY_FILE"}


# ---------------------------------------------------------------------------
# Journeys whose guide is a document
# ---------------------------------------------------------------------------
TODAY = datetime.date(2026, 10, 11)

DOCUMENT = """# A demo

## Setup

Run the script, then open http://localhost:3100 and click **Sign in**.
"""

JOURNEY: Dict[str, Any] = {
    "guide": "demo/GUIDE.md",
    "stack": "demo-local",
    "profile": "full",
    "video": False,
    "written": TODAY,
    "steps": [
        {
            "id": "dashboard",
            "doc": "setup",
            "do": {"open": "http://localhost:3100"},
            "expect": {"status": 200, "aria": '- link "Sign in"'},
            "fail": "the dashboard does not answer",
        }
    ],
}


def _document_context(tmp_path: Path, entry: Dict[str, Any]) -> loader.Context:
    (tmp_path / "docs").mkdir(exist_ok=True)
    (tmp_path / "demo").mkdir(exist_ok=True)
    (tmp_path / "demo" / "GUIDE.md").write_text(DOCUMENT, encoding="utf-8")
    return loader.Context(
        docs_root=tmp_path / "docs",
        inventory={"pages": {}, "documents": {"demo/GUIDE.md": entry}},
        doc_examples={},
        oracles={},
        today=TODAY,
    )


IN_SCOPE = {"scope": "in", "class": "journey", "journey": "guide", "reason": "walked"}


def _load_document(tmp_path: Path, entry: Dict[str, Any], data=None):
    path = tmp_path / "journeys" / "guide.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(data or JOURNEY, sort_keys=False), encoding="utf-8")
    return loader.load(path, _document_context(tmp_path, entry))


def test_a_journey_through_a_document_loads(tmp_path):
    journey = _load_document(tmp_path, IN_SCOPE)
    assert journey.guide == "demo/GUIDE.md"
    assert journey.stack == "demo-local"


@pytest.mark.parametrize(
    "entry, message",
    [
        (
            {"scope": "out", "out": "contributor", "reason": "x"},
            "a document inventory.toml puts out of scope",
        ),
        (
            {**IN_SCOPE, "pending": True, "planned": "D73"},
            "journey 'guide' is pending = true",
        ),
        ({**IN_SCOPE, "journey": "another"}, "names 'another' as the journey"),
        ({**IN_SCOPE, "class": "reference"}, "is classified 'reference'"),
    ],
    ids=["out-of-scope", "pending", "another-journey", "not-a-journey"],
)
def test_a_document_journey_the_inventory_does_not_hold_is_refused(
    tmp_path, entry, message
):
    with pytest.raises(loader.Refused) as refused:
        _load_document(tmp_path, entry)
    assert any(message in problem for problem in refused.value.problems), refused.value


def test_a_document_is_read_from_the_repository_root_not_docs(tmp_path):
    """A document's anchors and quotes are its own: the loader read demo/GUIDE.md."""
    data = copy.deepcopy(JOURNEY)
    data["steps"][0]["doc"] = "no-such-section"
    with pytest.raises(loader.Refused) as refused:
        _load_document(tmp_path, IN_SCOPE, data)
    assert "step 1 (dashboard): the guide has no anchor #no-such-section" in (
        refused.value.problems
    )


def test_a_ref_step_on_a_document_is_refused(tmp_path):
    data = copy.deepcopy(JOURNEY)
    data["steps"].append(
        {"id": "shell", "doc": "setup", "do": {"ref": "doc-examples"}, "fail": "x"}
    )
    with pytest.raises(loader.Refused) as refused:
        _load_document(tmp_path, IN_SCOPE, data)
    assert any(
        "ref: doc-examples is for a page under docs/" in problem
        for problem in refused.value.problems
    ), refused.value


def test_a_key_both_tables_hold_is_the_nav_page():
    context = loader.context_for(REPO_ROOT)
    assert "README.md" in context.inventory["pages"]
    assert "README.md" in context.inventory["documents"]
    assert loader.guide_root(context, "README.md") == REPO_ROOT / "docs"
    assert loader.guide_root(context, "demo/DEMO_GUIDE.md") == REPO_ROOT


def test_the_demo_documents_have_their_journeys():
    documents = loader.context_for(REPO_ROOT).inventory["documents"]
    journeys = sorted((RUNNER_ROOT / "journeys").glob("demo-*.yaml"))
    assert [path.stem for path in journeys] == [
        "demo-guide",
        "demo-shoplab",
        "demo-streampulse-simulator",
        "demo-streampulse",
    ]
    for path in journeys:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
        assert data["stack"] == "demo-local", path.name
        entry = documents[data["guide"]]
        assert entry["journey"] == path.stem and "pending" not in entry
    simulator = documents["demo/streampulse/simulator/README.md"]
    assert simulator["journey"] == "demo-streampulse-simulator"


# ---------------------------------------------------------------------------
# The session's stacks, and the scan of what a stack keeps
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def runner_conftest():
    spec = importlib.util.spec_from_file_location(
        "docs_journeys_conftest_under_test", RUNNER_ROOT / "conftest.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_by_default_a_session_runs_every_stack_but_demo_local(runner_conftest):
    chosen = runner_conftest.selected_stacks({})
    assert "demo-local" not in chosen
    assert set(chosen) == set(stacks.STACK_CLASSES) - {"demo-local"}


def test_the_demo_job_runs_demo_local_alone(runner_conftest):
    assert runner_conftest.selected_stacks({"DOCS_JOURNEY_STACKS": "demo-local"}) == (
        "demo-local",
    )
    selected = [
        path.stem
        for path in sorted((RUNNER_ROOT / "journeys").glob("*.yaml"))
        if runner_conftest.selected(
            path, ("demo-local",), {"DOCS_JOURNEY_STACKS": "demo-local"}
        )
    ]
    assert selected == [
        "demo-guide",
        "demo-shoplab",
        "demo-streampulse-simulator",
        "demo-streampulse",
    ]
    default = [
        path.stem
        for path in sorted((RUNNER_ROOT / "journeys").glob("*.yaml"))
        if runner_conftest.selected(path, runner_conftest.selected_stacks({}), {})
    ]
    assert not [stem for stem in default if stem.startswith("demo-")]


def test_an_unknown_stack_stops_the_session(runner_conftest):
    with pytest.raises(ValueError, match="names \\['demo'\\] stack"):
        runner_conftest.selected_stacks({"DOCS_JOURNEY_STACKS": "demo"})


def test_a_file_whose_stack_cannot_be_read_runs_in_the_default_session(
    runner_conftest, tmp_path
):
    broken = tmp_path / "broken.yaml"
    broken.write_text("stack: [\n", encoding="utf-8")
    assert runner_conftest.selected(broken, runner_conftest.selected_stacks({}), {})
    assert not runner_conftest.selected(
        broken, ("demo-local",), {"DOCS_JOURNEY_STACKS": "demo-local"}
    )


def _runner_copy(tmp_path: Path, test_body: str) -> Path:
    root = tmp_path / "repo"
    here = root / "tests" / "acceptance" / "docs"
    here.mkdir(parents=True)
    for name in ("conftest.py", "pytest.ini", "inventory.toml", "recordings.toml"):
        shutil.copy(RUNNER_ROOT / name, here / name)
    shutil.copytree(
        RUNNER_ROOT / "docs_runner",
        here / "docs_runner",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    (here / "test_journeys.py").write_text(test_body)
    (root / "scripts").mkdir()
    shutil.copy(REPO_ROOT / "scripts" / "doc_examples.toml", root / "scripts")
    shutil.copy(REPO_ROOT / "scripts" / "qa_render.py", root / "scripts")
    shutil.copytree(
        REPO_ROOT / ".github" / "qa-templates", root / ".github" / "qa-templates"
    )
    (root / "backend" / "tests").mkdir(parents=True)
    (root / "backend" / "__init__.py").write_text("")
    (root / "backend" / "tests" / "__init__.py").write_text("")
    shutil.copy(
        REPO_ROOT / "backend" / "tests" / "no_real_aws.py", root / "backend" / "tests"
    )
    return root


STACK_KEEPS = """import sys


def test_planted(pytestconfig):
    conftest = sys.modules["conftest"]

    class Stack:
        secrets = ["eptk_the_stack_keeps_this_0123"]

    {register}
    run_dir = conftest.run_dir_of(pytestconfig)
    (run_dir / "stacks").mkdir(parents=True, exist_ok=True)
    (run_dir / "stacks" / "planted.log").write_text("eptk_the_stack_keeps_this_0123")
"""


def _run_copy(root: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {**os.environ, "DOCS_JOURNEY_RUN_DIR": str(tmp_path / "run")}
    env.pop("PYTEST_ADDOPTS", None)
    env.pop("DOCS_JOURNEY_STACKS", None)
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-c",
            "tests/acceptance/docs/pytest.ini",
            "tests/acceptance/docs",
        ],
        cwd=root,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_the_scan_looks_for_what_a_stack_keeps(tmp_path):
    body = STACK_KEEPS.replace(
        "{register}", "pytestconfig.stash[conftest.STACKS_MADE].append(Stack())"
    )
    result = _run_copy(_runner_copy(tmp_path, body), tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert "stacks/planted.log held a value" in result.stdout
    assert not (tmp_path / "run" / "stacks" / "planted.log").exists()


def test_the_control_a_value_no_stack_keeps_is_not_looked_for(tmp_path):
    """The plant without the stack: the same file stays and the run passes, so
    the failure above is the stack's value being looked for."""
    body = STACK_KEEPS.replace("{register}", "pass")
    result = _run_copy(_runner_copy(tmp_path, body), tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "run" / "stacks" / "planted.log").exists()


STOP_FAILS = """import sys


def test_planted(pytestconfig):
    conftest = sys.modules["conftest"]
    pytestconfig.stash[conftest.STACK_PROBLEMS].append(
        "demo-local: the guide's stop command left: port 8000 still answers"
    )
"""


def test_a_stop_problem_fails_the_run(tmp_path):
    result = _run_copy(_runner_copy(tmp_path, STOP_FAILS), tmp_path)
    assert result.returncode == 1, result.stdout + result.stderr
    assert (
        "docs journeys: stack demo-local: the guide's stop command left: port 8000"
        " still answers; the run fails"
    ) in result.stdout
