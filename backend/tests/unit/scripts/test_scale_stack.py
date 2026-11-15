# SPDX-FileCopyrightText: 2026 The Controlvar authors
# SPDX-License-Identifier: Apache-2.0
"""The scale harness's isolation, override and fixtures (#1608).

``scripts/scale_stack.py`` runs every ``docker compose`` call the scale
harness makes. A reader who already runs Controlvar from ``deploy/compose``
must not have the harness touch that stack: its project is the one
``compose.yml`` names, and ``down -v`` deletes its database. Compose also lets
the OS environment win over ``--env-file``, so an exported ``POSTGRES_SERVER``
would point the scale stack at another database.

Nothing here runs Docker. Each test drives a real subcommand with the process
runner and the HTTP client replaced by :class:`World`, and asserts on what
would have run -- the argv, the whole child environment, the stdin -- and on
what is refused. ``deploy/compose/generate-env.sh`` is the exception: it runs
for real, with the environment the harness gives it, so the scrub is proven
by what the script writes and not only by what the harness passes.
"""

from __future__ import annotations

import ast
import http.server
import importlib.util
import io
import json
import os
import re
import stat
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[4]
SCRIPT = REPO_ROOT / "scripts" / "scale_stack.py"


def _load():
    spec = importlib.util.spec_from_file_location("scale_stack_under_test", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


ss = _load()

#: The project deploy/compose/compose.yml names: a real installation's.
SHIPPED_PROJECT = yaml.safe_load(ss.COMPOSE_FILE.read_text())["name"]

#: Exported in a reader's shell, each would change the scale stack.
PLANTED = {
    "POSTGRES_SERVER": "db.real.example",
    "POSTGRES_PASSWORD": "a-real-password",
    "WEB_CONCURRENCY": "2",
    "HTTP_PORT": "8080",
    "REDIS_HOST": "redis.real.example",
    "COMPOSE_FILE": "deploy/compose/compose.yml",
    "COMPOSE_PROJECT_NAME": SHIPPED_PROJECT,
    "PUBLIC_BASE_URL": "https://controlvar.real.example",
    "FIRST_SUPERUSER": "real-admin@real.example",
}


# -- the fake world -------------------------------------------------------------


class FakeApi:
    """The few routes ``fixtures`` uses, kept in memory."""

    def __init__(self, email: str, password: str) -> None:
        self.email, self.password = email, password
        self.token = "token-" + uuid.uuid4().hex
        self.flags: dict = {}
        self.experiments: list = []
        self.keys: list = []
        self.posts: list = []

    @staticmethod
    def _json(status: int, data) -> tuple:
        return status, {}, json.dumps(data).encode("utf-8")

    def handle(self, method, path, body, headers) -> tuple:
        if (method, path) == ("POST", "/api/v1/auth/login"):
            if body == {"email": self.email, "password": self.password}:
                return self._json(200, {"access_token": self.token})
            return self._json(401, {"detail": "Incorrect email or password"})
        if headers.get("Authorization") != f"Bearer {self.token}":
            return self._json(401, {"detail": "Not authenticated"})
        if method == "POST":
            self.posts.append(path)
        if method == "GET" and path.startswith("/api/v1/feature-flags/?"):
            return self._json(200, {"items": list(self.flags.values())})
        if (method, path) == ("POST", "/api/v1/feature-flags/"):
            flag = {"id": str(uuid.uuid4()), "status": "active", **body}
            self.flags[body["key"]] = flag
            return self._json(201, flag)
        if (method, path) == ("GET", "/api/v1/experiments/?limit=500"):
            return self._json(200, {"items": self.experiments})
        if (method, path) == ("POST", "/api/v1/experiments/"):
            experiment = {
                "id": str(uuid.uuid4()),
                "key": body["key"],
                "status": "draft",
                "variants": [
                    {"id": str(uuid.uuid4()), "name": v["name"]}
                    for v in body["variants"]
                ],
            }
            self.experiments.append(experiment)
            return self._json(201, experiment)
        start = re.fullmatch(r"/api/v1/experiments/([0-9a-f-]+)/start", path)
        if method == "POST" and start:
            for experiment in self.experiments:
                if experiment["id"] == start.group(1):
                    experiment["status"] = "active"
                    return self._json(200, experiment)
        if (method, path) == ("GET", "/api/v1/api-keys"):
            return self._json(
                200,
                [{k: v for k, v in key.items() if k != "key"} for key in self.keys],
            )
        if (method, path) == ("POST", "/api/v1/api-keys"):
            key = {
                "id": str(uuid.uuid4()),
                "name": body["name"],
                "scopes": body.get("scopes"),
                "is_active": True,
                "key": "cvk_" + uuid.uuid4().hex,
            }
            self.keys.append(key)
            return self._json(201, key)
        raise AssertionError(f"FakeApi: unexpected {method} {path}")


class World:
    """The processes and the HTTP server the harness would talk to."""

    def __init__(self, *, ready_after: int = 0, never_ready: bool = False) -> None:
        self.calls: list = []
        self.requests: list = []
        self.sleeps: list = []
        self.now = 1000.0
        self.ready_after = ready_after
        self.never_ready = never_ready
        self.compose_rc = 0
        self.psql_rc = 0
        self.real_status = 401
        self.real_body = {"detail": "Not authenticated"}
        self.limit: object = ss.SDK_RATE_LIMIT
        self.api: FakeApi | None = None
        self.out = io.StringIO()

    # processes
    def run(self, argv, env, input=None):
        argv = list(argv)
        self.calls.append((argv, dict(env), input))
        if argv[:2] == ["sh", str(ss.GENERATE_ENV)]:
            # The real script, given exactly the environment the harness built.
            return subprocess.run(
                argv,
                env=env,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
            )
        if argv[:3] == ["docker", "rm", "-f"]:
            return subprocess.CompletedProcess(argv, 1, b"", b"No such container")
        if argv[:2] == ["docker", "compose"]:
            if "exec" in argv:
                return subprocess.CompletedProcess(argv, self.psql_rc, b"loaded\n")
            return subprocess.CompletedProcess(argv, self.compose_rc)
        raise AssertionError(f"unexpected process {argv}")

    # http
    def http(self, method, url, host, body=None, headers=None, timeout=10.0):
        match = re.fullmatch(r"http://127\.0\.0\.1:(\d+)(/.*)", url)
        assert match, f"the harness connected to {url}, not its own 127.0.0.1 port"
        path = match.group(2)
        self.requests.append(
            {
                "method": method,
                "port": match.group(1),
                "path": path,
                "host": host,
                "headers": dict(headers or {}),
                "at": self.now,
            }
        )
        if path == ss.READY_PATH:
            ready = sum(r["path"] == ss.READY_PATH for r in self.requests)
            if self.never_ready or ready <= self.ready_after:
                return 503, {}, b""
            return 200, {}, b'{"status":"ready"}'
        if (method, path) in ss.REAL_REQUESTS:
            headers = {} if self.limit is None else {"x-ratelimit-limit": self.limit}
            return self.real_status, headers, json.dumps(self.real_body).encode()
        assert self.api is not None, f"no API for {method} {path}"
        return self.api.handle(method, path, body, dict(headers or {}))

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def clock(self):
        return self.now

    def io(self):
        return ss.Io(
            run=self.run,
            http=self.http,
            sleep=self.sleep,
            clock=self.clock,
            out=self.out,
        )

    def main(self, *argv) -> int:
        return ss.main([str(a) for a in argv], self.io())

    def compose_calls(self):
        return [c for c in self.calls if c[0][:2] == ["docker", "compose"]]


@pytest.fixture
def work(tmp_path):
    return tmp_path / "scale-work"


def _up(world: World, work: Path, *extra) -> int:
    return world.main("up", "--work", work, *extra)


def _with_api(world: World, work: Path) -> FakeApi:
    values = ss.read_env(work / ".env")
    world.api = FakeApi(values["FIRST_SUPERUSER"], values["FIRST_SUPERUSER_PASSWORD"])
    return world.api


def _fixtures(world: World, work: Path, users=10, events=20, raw_metrics=30) -> int:
    return world.main(
        "fixtures",
        "--work",
        work,
        "--users",
        users,
        "--events",
        events,
        "--raw-metrics",
        raw_metrics,
    )


def _call_name(argv) -> str:
    """generate-env.sh, or docker compose and its command."""
    if argv[:2] == ["sh", str(ss.GENERATE_ENV)]:
        return "generate-env.sh"
    if argv[:3] == ["docker", "rm", "-f"]:
        return "docker rm"
    command = next((a for a in argv[2:] if a in {"up", "exec", "down"}), "?")
    return f"docker compose {command}"


def _whole_session(world: World, work: Path) -> None:
    """up, fixtures and down, each of which must succeed."""
    assert _up(world, work) == 0
    _with_api(world, work)
    assert _fixtures(world, work) == 0
    assert world.main("down", "--work", work) == 0


# -- V36: one project, one env file, nothing from the shell ---------------------


def test_the_compose_argv_names_the_project_the_env_file_and_both_files(work):
    assert ss.compose_argv(work, "ps") == [
        "docker",
        "compose",
        "-p",
        "controlvar-scale",
        "--env-file",
        str(work / ".env"),
        "-f",
        str(REPO_ROOT / "deploy" / "compose" / "compose.yml"),
        "-f",
        str(REPO_ROOT / "tests" / "scale" / "compose.scale.yml"),
        "ps",
    ]


def test_every_compose_call_carries_the_project_and_its_own_env_file(work):
    world = World()
    _whole_session(world, work)
    calls = world.compose_calls()
    for argv, _, _ in calls:
        assert argv[2:4] == ["-p", "controlvar-scale"], f"no -p in {argv}"
        assert argv[4:6] == ["--env-file", str(work.resolve() / ".env")], argv
        assert argv[6:10] == ["-f", str(ss.COMPOSE_FILE), "-f", str(ss.OVERRIDE_FILE)]
    # up, the preload's exec, down
    assert [c[0][10] for c in calls] == ["up", "exec", "down"]
    assert calls[-1][0][10:] == ["down", "-v"]
    # Every process is either compose or generate-env.sh: nothing else runs.
    assert {tuple(c[0][:2]) for c in world.calls} == {
        ("docker", "rm"),
        ("docker", "compose"),
        ("sh", str(ss.GENERATE_ENV)),
    }


@pytest.mark.parametrize("name", sorted(PLANTED))
def test_the_child_environment_carries_nothing_from_the_shell(work, monkeypatch, name):
    for key, value in PLANTED.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("DOCKER_HOST", "unix:///var/run/docker.sock")
    world = World()
    _whole_session(world, work)
    calls = [_call_name(argv) for argv, _, _ in world.calls]
    assert calls == [
        "generate-env.sh",
        "docker compose up",
        "docker compose exec",
        "docker rm",
        "docker rm",
        "docker rm",
        "docker compose down",
    ]
    for call, (_, env, _) in zip(calls, world.calls):
        assert name not in env, f"child environment of {call} carries {name}"
        assert env.get("DOCKER_HOST") == "unix:///var/run/docker.sock", call


def test_down_removes_the_harness_s_own_fixed_name_containers_before_the_network(work):
    """N8: the k6, database-sampler and stub containers sit on the project's
    network under fixed names; a killed run leaves them, and compose down
    cannot remove the network from under them."""
    assert ss.HARNESS_CONTAINERS == (
        "controlvar-scale-k6",
        "controlvar-scale-pg-sampler",
        "controlvar-scale-stub",
    )
    world = World()
    assert _up(world, work) == 0
    _with_api(world, work)
    before = len(world.calls)
    assert world.main("down", "--work", work) == 0
    down = [argv for argv, _, _ in world.calls[before:]]
    assert down[:3] == [["docker", "rm", "-f", n] for n in ss.HARNESS_CONTAINERS]
    assert down[3][:2] == ["docker", "compose"] and down[3][10:] == ["down", "-v"]
    assert len(down) == 4


def test_a_child_environment_is_the_keep_list_and_only_what_the_harness_sets(
    work, monkeypatch
):
    for key, value in PLANTED.items():
        monkeypatch.setenv(key, value)
    world = World()
    _whole_session(world, work)
    kept = {k: os.environ[k] for k in ss.KEEP_ENV if k in os.environ}
    generate, *compose = world.calls
    assert generate[0] == ["sh", str(ss.GENERATE_ENV)]
    expected = {**kept, "ENV_FILE": str(work.resolve() / ".env.generating")}
    assert generate[1] == expected, (
        f"child environment of generate-env.sh carries "
        f"{sorted(set(generate[1]) - set(expected))}"
    )
    for argv, env, _ in compose:
        assert env == kept, (
            f"child environment of {_call_name(argv)} carries "
            f"{sorted(set(env) - set(kept))}"
        )


#: A child that writes the environment it was started with to argv[1].
_DUMP_ENVIRONMENT = (
    "import json, os, sys\n"
    "with open(sys.argv[1], 'w') as out:\n"
    "    json.dump(dict(os.environ), out)\n"
)


@pytest.mark.parametrize("stdin", [None, b""], ids=["no input", "an input"])
def test_the_real_process_runner_starts_a_child_with_only_what_it_is_given(
    tmp_path, monkeypatch, stdin
):
    """Every other test replaces Io.run, so this is the one that runs the
    real one: what reaches a real child is what the harness built, and
    nothing from the shell."""
    for key, value in PLANTED.items():
        monkeypatch.setenv(key, value)
    assert ss.Io().run is ss.run_process
    dump = tmp_path / "environment.json"
    result = ss.run_process(
        [sys.executable, "-I", "-c", _DUMP_ENVIRONMENT, str(dump)],
        ss.child_env({"ENV_FILE": "set-by-the-harness"}),
        input=stdin,
    )
    assert result.returncode == 0
    seen = json.loads(dump.read_text())
    # Not an exact match: on macOS a Python child adds LC_CTYPE and
    # __CF_USER_TEXT_ENCODING to its own environment.
    leaked = sorted(set(PLANTED) & set(seen))
    assert not leaked, f"leaked into the real child: {leaked}"
    assert seen["ENV_FILE"] == "set-by-the-harness"
    for name in ss.KEEP_ENV:
        assert seen.get(name) == os.environ.get(name), name


def test_the_shell_cannot_reach_the_env_file_generate_env_writes(work, monkeypatch):
    """generate-env.sh reads PUBLIC_BASE_URL and FIRST_SUPERUSER (and the
    profile) from its environment: planted, they must not land."""
    for key, value in PLANTED.items():
        monkeypatch.setenv(key, value)
    assert _up(World(), work) == 0
    values = ss.read_env(work / ".env")
    assert values["FIRST_SUPERUSER"] == "admin@example.com"
    assert values["PUBLIC_BASE_URL"] == "http://localhost:18080"
    assert values["HTTP_PORT"] == "18080"
    for key in ("POSTGRES_SERVER", "WEB_CONCURRENCY", "REDIS_HOST", "COMPOSE_FILE"):
        assert key not in values, f"the env file took {key} from the shell"


def test_the_keep_list_is_check_compose_configs():
    """One list for both callers of docker compose with a scrubbed
    environment: scripts/check_compose_config.py's ``keep``."""
    tree = ast.parse((REPO_ROOT / "scripts" / "check_compose_config.py").read_text())
    keeps = [
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and [getattr(t, "id", None) for t in node.targets] == ["keep"]
    ]
    assert len(keeps) == 1
    assert ast.literal_eval(keeps[0]) == ss.KEEP_ENV


@pytest.mark.parametrize(
    "project",
    [SHIPPED_PROJECT, f" {SHIPPED_PROJECT.title()} ", "controlvar"],
    ids=["compose.yml's own", "its own, re-cased", "another"],
)
def test_only_the_harness_project_is_accepted(work, project):
    with pytest.raises(ss.Refused) as refused:
        ss.compose_argv(work, "down", "-v", project=project)
    if project.strip().lower() == SHIPPED_PROJECT:
        assert SHIPPED_PROJECT in str(refused.value)


def test_the_refused_project_is_read_from_compose_yml():
    assert ss.selfhost_project() == SHIPPED_PROJECT != ss.PROJECT


@pytest.mark.parametrize(
    "name_line", ["name: controlvar-scale\n", "# no name line\n"], ids=["ours", "none"]
)
def test_a_compose_yml_naming_our_project_or_none_is_refused(
    work, tmp_path, monkeypatch, name_line
):
    """Were compose.yml ever to name its project controlvar-scale, -p would
    no longer separate the two stacks; with no name, nothing says which
    project is the real one."""
    shipped = ss.COMPOSE_FILE.read_text()
    fake = tmp_path / "compose.yml"
    fake.write_text(re.sub(r"(?m)^name: .*\n", name_line, shipped, count=1))
    assert fake.read_text().count("\n") == shipped.count("\n") and (
        fake.read_text() != shipped
    ), "the name line was not replaced"
    monkeypatch.setattr(ss, "COMPOSE_FILE", fake)
    with pytest.raises(ss.Refused):
        ss.compose_argv(work, "up", "-d")


def _selfhost_dir_link(tmp_path):
    link = tmp_path / "looks-harmless"
    link.symlink_to(ss.SELFHOST_ENV_FILE.parent, target_is_directory=True)
    return link


def _selfhost_env_link(tmp_path):
    work = tmp_path / "linked-env"
    work.mkdir()
    (work / ".env").symlink_to(ss.SELFHOST_ENV_FILE)
    return work


@pytest.mark.parametrize(
    "make_work",
    [
        lambda tmp: ss.SELFHOST_ENV_FILE.parent,
        _selfhost_dir_link,
        _selfhost_env_link,
    ],
    ids=["deploy/compose", "a link to it", "a .env linking to its .env"],
)
@pytest.mark.parametrize("command", ["up", "fixtures", "down"])
def test_the_self_hosted_env_file_is_refused(tmp_path, capsys, make_work, command):
    existed = ss.SELFHOST_ENV_FILE.exists()
    world = World()
    extra = ["--users", 1, "--events", 0, "--raw-metrics", 0]
    status = world.main(
        command,
        "--work",
        make_work(tmp_path),
        *(extra if command == "fixtures" else []),
    )
    assert status == 2
    assert "deploy/compose" in capsys.readouterr().err
    assert world.calls == [] and world.requests == []
    assert ss.SELFHOST_ENV_FILE.exists() == existed


def test_deploy_compose_spelled_in_another_case_is_refused():
    """On a case-insensitive filesystem (macOS by default) ``DEPLOY/Compose``
    is deploy/compose, and ``resolve()`` keeps the case it was given."""
    recased = REPO_ROOT / "DEPLOY" / "Compose"
    if not recased.is_dir():
        pytest.skip("a case-sensitive filesystem: DEPLOY/Compose is not deploy/compose")
    with pytest.raises(ss.Refused, match=r"is deploy/compose, the self-hosted"):
        ss.work_dir(recased)


@pytest.mark.parametrize("name", ss.COMPOSE_FILE_NAMES)
def test_a_work_directory_holding_a_compose_file_is_refused(tmp_path, capsys, name):
    """Another checkout's deploy/compose, or any Compose project's directory:
    its .env may be a real installation's."""
    other = tmp_path / "another-checkout" / "deploy" / "compose"
    other.mkdir(parents=True)
    (other / name).write_text(ss.COMPOSE_FILE.read_text())
    world = World()
    assert world.main("up", "--work", other) == 2
    assert f"holds {name}" in capsys.readouterr().err
    assert world.calls == [] and world.requests == []
    assert [p.name for p in other.iterdir()] == [name]


@pytest.mark.parametrize("command", ["up", "fixtures", "down"])
def test_an_env_file_the_harness_did_not_write_is_never_used(work, capsys, command):
    work.mkdir()
    foreign = "POSTGRES_PASSWORD=real\nFIRST_SUPERUSER_PASSWORD=real\nHTTP_PORT=8080\n"
    (work / ".env").write_text(foreign)
    world = World()
    extra = ["--users", 1, "--events", 0, "--raw-metrics", 0]
    status = world.main(
        command, "--work", work, *(extra if command == "fixtures" else [])
    )
    assert status == 2
    assert "not written by this harness" in capsys.readouterr().err
    assert world.calls == [] and world.requests == []
    assert (work / ".env").read_text() == foreign


def test_there_is_no_way_to_aim_it_at_another_target(work):
    """PE-S19: no --target-url, no --host-header, no project option."""
    options = [
        option
        for action in ss.parser()._subparsers._group_actions
        for command in action.choices.values()
        for option in command._option_string_actions
    ]
    assert set(options) == {
        "-h",
        "--help",
        "--work",
        "--http-port",
        "--users",
        "--events",
        "--raw-metrics",
    }
    with pytest.raises(SystemExit) as exited:
        ss.parser().parse_args(["up", "--work", str(work), "--target-url", "x"])
    assert exited.value.code == 2


# -- up -------------------------------------------------------------------------


def test_up_writes_its_env_file_once_and_privately(work):
    world = World()
    assert _up(world, work) == 0
    path = work / ".env"
    text = path.read_text()
    values = ss.read_env(path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(work.stat().st_mode) == 0o700
    assert text.count("PUBLIC_BASE_URL=") == 1
    assert values[ss.ENV_MARKER] == "controlvar-scale"
    assert re.fullmatch(r"[0-9a-f]{64}", values["SCALE_METRICS_TOKEN"])
    for name in ("SECRET_KEY", "POSTGRES_PASSWORD", "FIRST_SUPERUSER_PASSWORD"):
        assert values[name], name
    assert not (work / ".env.generating").exists()

    again = World()
    assert _up(again, work) == 0
    assert [c[0][:2] for c in again.calls] == [["docker", "compose"]]
    assert path.read_text() == text


def test_up_polls_readiness_slowly_then_sends_each_real_request(work):
    world = World(ready_after=3)
    assert _up(world, work, "--http-port", 18123) == 0
    ready = [r for r in world.requests if r["path"] == ss.READY_PATH]
    assert len(ready) == 4
    assert world.sleeps == [ss.READY_POLL_S] * 3
    real = [r for r in world.requests if r["path"] != ss.READY_PATH]
    assert [(r["method"], r["path"]) for r in real] == list(ss.REAL_REQUESTS)
    for request in world.requests:
        assert request["port"] == "18123"
        assert request["host"] == "localhost:18123"
        assert "Authorization" not in request["headers"]


def test_up_reports_a_refused_host(work, capsys):
    world = World()
    world.real_status, world.real_body = 400, {"detail": "Invalid host header"}
    assert _up(world, work) == 1
    assert "PUBLIC_BASE_URL" in capsys.readouterr().err


@pytest.mark.parametrize("limit", [None, "6000"])
def test_up_reports_a_limit_that_is_not_the_overrides(work, capsys, limit):
    world = World()
    world.limit = limit
    assert _up(world, work) == 1
    err = capsys.readouterr().err
    assert "X-RateLimit-Limit" in err and "override" in err


def test_up_stops_when_compose_fails(work):
    world = World()
    world.compose_rc = 1
    assert _up(world, work) == 1
    assert world.requests == []


def test_up_gives_up_at_the_readiness_deadline(work, capsys):
    world = World(never_ready=True)
    assert _up(world, work) == 1
    assert len(world.sleeps) == ss.READY_TIMEOUT_S // ss.READY_POLL_S
    assert "/health/ready" in capsys.readouterr().err


def test_up_keeps_the_port_it_was_set_up_with(work):
    assert _up(World(), work) == 0
    world = World()
    assert _up(world, work, "--http-port", 18081) == 2
    assert world.calls == []


@pytest.mark.parametrize("port", [80, 70000])
def test_up_refuses_a_port_out_of_range(work, port):
    world = World()
    assert _up(world, work, "--http-port", port) == 2
    assert world.calls == [] and not (work / ".env").exists()


# -- fixtures -------------------------------------------------------------------


def test_fixtures_creates_everything_through_the_api_at_four_a_second(work, capsys):
    world = World()
    assert _up(world, work) == 0
    api = _with_api(world, work)
    assert _fixtures(world, work) == 0

    assert sorted(api.flags) == [f"scale_flag_{i:03d}" for i in range(100)]
    ruled = sorted(k for k, f in api.flags.items() if "targeting_rules" in f)
    assert ruled == [f"scale_flag_{i:03d}" for i in range(50)]
    assert all(api.flags[key]["targeting_rules"] == ss.RULE for key in ruled)
    assert [e["status"] for e in api.experiments] == ["active"]
    assert {k["name"]: k["scopes"] for k in api.keys} == {
        "scale-sdk": None,
        "scale-server": ["sdk:ruleset"],
    }

    calls = [r for r in world.requests if r["path"].startswith("/api/v1/")]
    calls = [r for r in calls if (r["method"], r["path"]) not in ss.REAL_REQUESTS]
    gaps = [b["at"] - a["at"] for a, b in zip(calls, calls[1:])]
    assert len(calls) > 100 and min(gaps) >= 0.25 - 1e-9

    path = work / "fixtures.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    fixtures = json.loads(path.read_text())
    assert fixtures["api_keys"] == {
        k["name"]: {"id": k["id"], "key": k["key"]} for k in api.keys
    }
    assert fixtures["flags"]["rule"] == ruled
    assert len(fixtures["flags"]["plain"]) == 50
    assert fixtures["rule_context"] == ss.RULE_CONTEXT
    assert [v["name"] for v in fixtures["experiment"]["variants"]] == [
        "control",
        "treatment",
    ]
    assert fixtures["returning_users"] == {
        "prefix": "scale-user-",
        "width": 7,
        "count": 10,
    }

    printed = world.out.getvalue() + "".join(capsys.readouterr())
    for key in api.keys:
        assert key["key"] not in printed, "an API key was printed"


def test_fixtures_twice_creates_nothing_the_second_time(work):
    world = World()
    assert _up(world, work) == 0
    api = _with_api(world, work)
    assert _fixtures(world, work) == 0
    first = json.loads((work / "fixtures.json").read_text())
    api.posts.clear()
    assert _fixtures(world, work) == 0
    assert api.posts == []
    assert json.loads((work / "fixtures.json").read_text()) == first


def test_fixtures_makes_new_keys_for_a_stack_that_lost_them(work):
    world = World()
    assert _up(world, work) == 0
    api = _with_api(world, work)
    assert _fixtures(world, work) == 0
    old = json.loads((work / "fixtures.json").read_text())["api_keys"]
    api.keys.clear()  # a stack that was taken down (-v) and brought up again
    assert _fixtures(world, work) == 0
    new = json.loads((work / "fixtures.json").read_text())["api_keys"]
    assert set(new) == set(old)
    assert all(new[name]["key"] != old[name]["key"] for name in new)


@pytest.mark.parametrize("status", ["paused", "draft", "completed"])
def test_fixtures_refuses_a_scale_ab_that_is_not_active(work, capsys, status):
    world = World()
    assert _up(world, work) == 0
    api = _with_api(world, work)
    variants = [{"id": str(uuid.uuid4()), "name": n} for n in ss.VARIANT_NAMES]
    api.experiments.append(
        {
            "id": str(uuid.uuid4()),
            "key": "scale_ab",
            "status": status,
            "variants": variants,
        }
    )
    assert _fixtures(world, work) == 2
    assert f"scale_ab exists but is {status}" in capsys.readouterr().err
    assert not any("exec" in c[0] for c in world.calls), "the preload ran"


def test_fixtures_refuses_a_scale_flag_that_is_not_active(work, capsys):
    world = World()
    assert _up(world, work) == 0
    api = _with_api(world, work)
    api.flags["scale_flag_007"] = {"key": "scale_flag_007", "status": "inactive"}
    assert _fixtures(world, work) == 2
    assert "scale_flag_007 exists but is inactive" in capsys.readouterr().err


def test_fixtures_stops_on_a_wrong_password(work, capsys):
    world = World()
    assert _up(world, work) == 0
    world.api = FakeApi("admin@example.com", "not-the-password")
    assert _fixtures(world, work) == 1
    assert "/api/v1/auth/login answered 401" in capsys.readouterr().err
    assert not (work / "fixtures.json").exists()


def test_the_preload_is_the_files_bytes_then_one_call(work):
    world = World()
    _whole_session(world, work)
    (argv, env, stdin) = next(c for c in world.calls if "exec" in c[0])
    assert argv == ss.compose_argv(
        work.resolve(),
        "exec",
        "-T",
        "postgres",
        "sh",
        "-c",
        'exec psql -X -q -t -A -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" '
        '-d "$POSTGRES_DB" -f -',
    )
    sql = ss.PRELOAD_SQL.read_bytes()
    assert stdin[: len(sql)] == sql
    assert stdin[len(sql) :] == (
        b"\nSELECT pg_temp.scale_preload('experimentation', 10, 20, 30, "
        b"'scale-user-', 7);\n"
    )


@pytest.mark.parametrize(
    "counts", [(0, 1, 1), (1, -1, 1), (1, 1, -1), (True, 1, 1), (1, 1.5, 1)]
)
def test_the_preload_counts_are_whole_numbers(counts):
    with pytest.raises(ss.Refused):
        ss.preload_input(*counts)


def test_the_preload_call_names_the_schema_it_is_given():
    """The integration test runs these bytes against its own schema."""
    assert ss.preload_input(1, 0, 0, schema="test_experimentation").endswith(
        b"\nSELECT pg_temp.scale_preload('test_experimentation', 1, 0, 0, "
        b"'scale-user-', 7);\n"
    )


@pytest.mark.parametrize("schema", ["x'y", "Experimentation", "a b", "a;b", ""])
def test_the_preload_schema_is_a_plain_name(schema):
    with pytest.raises(ss.Refused):
        ss.preload_input(1, 0, 0, schema=schema)


def test_the_preload_orders_the_variants_in_byte_order():
    """The rule is variant (i mod 2) in byte order of name. The integration
    job's PostgreSQL uses musl, whose default collation is byte order
    already, so it passes without the COLLATE; this pins the text."""
    count = ss.PRELOAD_SQL.read_text().count('ORDER BY name COLLATE "C"')
    assert count == 1, (
        f'preload.sql orders the variants by name COLLATE "C" {count} times, not once'
    )


def test_a_failed_preload_fails_the_command(work):
    world = World()
    assert _up(world, work) == 0
    _with_api(world, work)
    world.psql_rc = 3
    assert _fixtures(world, work) == 1


# -- the fixtures are what the API accepts, and the rule context matches -------


def test_the_fixture_payloads_pass_the_apis_own_schemas():
    from backend.app.schemas.api_key import APIKeyCreate
    from backend.app.schemas.experiment import ExperimentCreate
    from backend.app.schemas.feature_flag import FeatureFlagCreate

    for payload in ss.flag_payloads():
        FeatureFlagCreate(**payload)
    ExperimentCreate(**ss.experiment_payload())
    assert APIKeyCreate(**ss.api_key_payload("scale-server")).scopes == ["sdk:ruleset"]
    assert APIKeyCreate(**ss.api_key_payload("scale-sdk")).scopes is None


def test_the_rule_context_matches_the_rule_and_a_partial_one_does_not():
    from backend.app.core.targeting_adapter import (
        expand_context,
        match_targeting_rule,
        normalise_targeting_rules,
    )

    rules = normalise_targeting_rules(ss.RULE, owner="test")
    assert rules is not None
    assert match_targeting_rule(rules, expand_context(ss.RULE_CONTEXT)) is not None
    for missing in ss.RULE_CONTEXT:
        partial = {k: v for k, v in ss.RULE_CONTEXT.items() if k != missing}
        assert match_targeting_rule(rules, expand_context(partial)) is None, missing


# -- the override ----------------------------------------------------------------


def _override() -> dict:
    return yaml.safe_load(ss.OVERRIDE_FILE.read_text())


def test_the_override_changes_only_what_it_says():
    override = _override()
    assert set(override) == {"services"}, "the override sets more than services"
    services = override["services"]
    shipped = yaml.safe_load(ss.COMPOSE_FILE.read_text())["services"]
    assert set(services) == {"api", "postgres", "redis", "web"} == set(shipped)

    api = services["api"]
    assert set(api) == {"cpuset", "environment"}, f"the override sets api's {set(api)}"
    extra = set(api["environment"]) - {"SDK_RATE_LIMIT_PER_MINUTE", "METRICS_TOKEN"}
    assert not extra, f"the override changes the api's {sorted(extra)}"
    assert api["environment"]["SDK_RATE_LIMIT_PER_MINUTE"] == ss.SDK_RATE_LIMIT
    assert api["environment"]["METRICS_TOKEN"].startswith("${SCALE_METRICS_TOKEN:?")
    for name in ("postgres", "redis", "web"):
        assert set(services[name]) == {"cpuset"}, f"the override changes {name}"


def _cpus(cpuset) -> set:
    """The CPUs a cpuset string names: ``"1"``, ``"0,2"``, ``"0-2"``."""
    cpus = set()
    for part in str(cpuset).split(","):
        low, _, high = part.strip().partition("-")
        cpus.update(range(int(low), int(high or low) + 1))
    return cpus


def test_the_stack_runs_on_cpus_0_and_1_and_leaves_2_and_3_to_the_harness():
    """The api on CPU 0 and the rest of the stack on its sibling, CPU 1;
    CPUs 2 and 3 (the other pair of hardware threads on the runner probed)
    are for the samplers, the harness and the load generator."""
    services = _override()["services"]
    cpusets = {name: str(service["cpuset"]) for name, service in services.items()}
    assert cpusets == {"api": "0", "postgres": "1", "redis": "1", "web": "1"}
    on_theirs = {name: sorted(_cpus(c) & {2, 3}) for name, c in cpusets.items()}
    assert not any(on_theirs.values()), (
        f"the system under test is on the harness's CPUs: {on_theirs}"
    )


def _volume_source(entry):
    """A service volume's (type, source): the short ``source:target[:mode]``
    or the long form. An anonymous volume has no source."""
    if isinstance(entry, dict):
        return entry.get("type"), entry.get("source")
    source, colon, _ = str(entry).partition(":")
    return "volume", source if colon else None


def test_the_project_name_alone_keeps_the_scale_stack_apart():
    """``-p controlvar-scale`` separates the scale stack from a real
    installation only because neither file names a container, a volume or a
    network itself: Compose prefixes each of those with the project. A
    ``container_name``, a volume or network with its own ``name:`` or
    ``external:``, or a bind mount is the same one whatever the project, so
    the scale stack could start against, load its fixtures into, or (for a
    named volume) ``down -v`` remove a real installation's."""
    paths = (ss.COMPOSE_FILE, ss.OVERRIDE_FILE)
    files = {path.name: yaml.safe_load(path.read_text()) for path in paths}
    problems, declared, mounts = [], set(), []
    for file, data in files.items():
        for kind in ("volumes", "networks"):
            for name, spec in (data.get(kind) or {}).items():
                if kind == "volumes":
                    declared.add(name)
                for key in sorted(set(spec or {}) & {"name", "external"}):
                    problems.append(f"{file}: the {kind[:-1]} {name} sets {key}")
    for file, data in files.items():
        for service, spec in (data.get("services") or {}).items():
            if "container_name" in spec:
                problems.append(f"{file}: {service} sets container_name")
            for entry in spec.get("volumes") or []:
                kind, source = _volume_source(entry)
                if kind != "volume" or source not in declared:
                    problems.append(
                        f"{file}: {service} mounts {entry!r}, not a named volume "
                        f"the files declare"
                    )
                else:
                    mounts.append((service, source))
    assert not problems, problems
    assert ("postgres", "postgres_data") in mounts, mounts


# -- the HTTP client --------------------------------------------------------------


class _Recorder(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_GET(self):
        type(self).seen.append(self.headers.get("Host"))
        self.send_response(302)
        self.send_header("Location", "http://127.0.0.1:1/elsewhere")
        self.send_header("X-RateLimit-Limit", "10000000")
        self.end_headers()

    def log_message(self, *args):
        pass


def test_the_http_client_sends_the_host_it_is_given_and_follows_no_redirect():
    server = http.server.HTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        status, headers, _ = ss.http_request(
            "GET", f"http://127.0.0.1:{port}/x", host="localhost:18080"
        )
    finally:
        server.shutdown()
        server.server_close()
    assert _Recorder.seen == ["localhost:18080"]
    assert status == 302
    assert headers["x-ratelimit-limit"] == "10000000"


def test_the_http_client_reports_no_answer_as_status_zero():
    assert ss.http_request("GET", "http://127.0.0.1:1/", host="localhost:1")[0] == 0
