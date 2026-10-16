"""The Quick Start stack in the showcase tool (E1a of T166).

The image tag, compose's environment, the published ports, the project's
record and its recovery, the per-video override list, the server key read
into memory, the paced traffic, the 429 gate and the render's refusal of any
stack but quickstart. Each guard is shown refusing its planted defect.

No Docker: a stand-in ``docker`` on ``PATH`` answers from a script and writes
every call it gets (its argv and the names in its environment) to a log.
"""

from __future__ import annotations

import json
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List

import pytest
from showcase import contract
from showcase.capture import (
    compose,
    config,
    director,
    guard,
    run,
    storyboard,
    traffic,
)
from showcase.capture import overrides as override_lists
from showcase.capture.procs import Children
from showcase.capture.stack import Ports, StackError
from showcase.render import pipeline
from showcase.render.gates import Refused

from backend.tests.unit.showcase.capture_dirs import edit_json, make_capture

pytestmark = [pytest.mark.unit]

REPO_ROOT = Path(__file__).resolve().parents[4]
CAPTURE = REPO_ROOT / "tests" / "acceptance" / "showcase" / "capture"
PORTS = Ports(28400, 28401, 28402, 28403, 28404, 28405)
COMMIT = "3adcb908" + "0" * 32
SERVER_KEY = "eptk_" + "s" * 40
API_ID = "sha256:" + "a" * 64
WEB_ID = "sha256:" + "b" * 64

# --- A stand-in docker -------------------------------------------------------------

FAKE_DOCKER = """#!{python}
import json, os, sys
here = os.path.dirname(os.path.abspath(__file__))
path = os.path.join(here, "script.json")
with open(path) as handle:
    script = json.load(handle)
argv = sys.argv[1:]
said = " ".join(argv)
line = {{"argv": argv, "env": sorted(os.environ)}}
records = script.get("records")
if records and "up" in argv:
    line["records"] = sorted(os.listdir(records)) if os.path.isdir(records) else []
with open(os.path.join(here, "calls.jsonl"), "a") as handle:
    handle.write(json.dumps(line) + "\\n")
for index, (needle, answer) in enumerate(script["answers"]):
    if needle in said:
        if answer.get("once"):
            del script["answers"][index]
            with open(path, "w") as handle:
                json.dump(script, handle)
        sys.stdout.write(answer.get("out", ""))
        sys.stderr.write(answer.get("err", ""))
        sys.exit(answer.get("status", 0))
sys.exit(0)
"""


class Fake:
    """The stand-in docker: its directory, its script and its log."""

    def __init__(self, tmp_path: Path):
        self.bin = tmp_path / "bin"
        self.bin.mkdir()
        docker = self.bin / "docker"
        docker.write_text(FAKE_DOCKER.format(python=sys.executable), encoding="utf-8")
        docker.chmod(docker.stat().st_mode | stat.S_IEXEC)
        self.answers: List[Any] = []
        self.records = ""

    def write(self) -> None:
        (self.bin / "script.json").write_text(
            json.dumps({"answers": self.answers, "records": self.records}),
            encoding="utf-8",
        )

    def calls(self) -> List[Dict[str, Any]]:
        log = self.bin / "calls.jsonl"
        if not log.is_file():
            return []
        return [json.loads(line) for line in log.read_text().splitlines()]

    def said(self) -> List[str]:
        return [" ".join(c["argv"]) for c in self.calls() if "argv" in c]

    def parent(self) -> Dict[str, str]:
        return {
            "PATH": f"{self.bin}:/usr/bin:/bin",
            "HOME": "/nowhere",
            "TMPDIR": "/tmp",
        }


def ps_lines(project: str, ports: Ports = PORTS, **changed) -> str:
    published = {
        "postgres": [("127.0.0.1", ports.postgres, 5432)],
        "redis": [("127.0.0.1", ports.redis, 6379)],
        "api": [("127.0.0.1", ports.api, 8000)],
        "frontend": [("127.0.0.1", ports.dashboard, 8080)],
    }
    published.update(changed)
    tag = f"showcase-core-{COMMIT[:12]}"
    images = {
        "api": f"experimently-api:{tag}",
        "frontend": f"experimently-web:{tag}",
        "postgres": "postgres:16-alpine",
        "redis": "redis:7-alpine",
    }
    lines = []
    for service, entries in published.items():
        lines.append(
            json.dumps(
                {
                    "Service": service,
                    "ID": f"{service}-container-id",
                    "Image": images.get(service, f"{service}:x"),
                    "Name": f"{project}-{service}-1",
                    "Publishers": [
                        {"URL": url, "PublishedPort": port, "TargetPort": target}
                        for url, port, target in entries
                    ]
                    + [{"URL": "", "PublishedPort": 0, "TargetPort": 9}],
                }
            )
        )
    return "\n".join(lines) + "\n"


def inspect(image_id: str, profile: str) -> Dict[str, Any]:
    return {
        "out": json.dumps(
            {
                "Id": image_id,
                "Config": {"Labels": {config.IMAGE_PROFILE_LABEL: profile}},
            }
        )
    }


def make_stack(tmp_path: Path, fake: Fake, *, profile="core", apps=None, **kw):
    work = guard.WorkRoot(tmp_path / "work")
    work.path.mkdir(parents=True, exist_ok=True)
    tree = work.path / "tree-core"
    tree.mkdir(exist_ok=True)
    (tree / "docker-compose.yml").write_text(
        (REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    logs = work.path / "v" / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    fake.records = str(work.path / compose.PROJECTS_DIR)
    fake.write()
    children = Children(work, fake.parent())
    stack = compose.QuickstartStack(
        tree=tree,
        ports=PORTS,
        profile=profile,
        commit=COMMIT,
        run_id="20261015120000-abc123",
        children=children,
        logs=logs,
        parent=fake.parent(),
        apps=apps,
        **kw,
    )
    return stack, work


def healthy_answers(project: str, *, before=None) -> List[Any]:
    """A clean up: no project yet, images built by up, the four ports published."""
    tag = f"showcase-core-{COMMIT[:12]}"
    answers: List[Any] = [["compose ls -a", {"out": "[]"}]]
    if before is None:
        answers += [
            [
                f"image inspect --format {{{{json .}}}} experimently-api:{tag}",
                {"status": 1, "once": True},
            ],
            [
                f"image inspect --format {{{{json .}}}} experimently-web:{tag}",
                {"status": 1, "once": True},
            ],
        ]
    else:
        answers += before
    answers += [
        [f"experimently-api:{tag}", inspect(API_ID, "core")],
        [f"experimently-web:{tag}", inspect(WEB_ID, "core")],
        ["ps --format json", {"out": ps_lines(project)}],
        ["inspect --format {{.Image}} api-container-id", {"out": API_ID + "\n"}],
        ["inspect --format {{.Image}} frontend-container-id", {"out": WEB_ID + "\n"}],
    ]
    return answers


@pytest.fixture
def served(monkeypatch):
    """The API answers the profile asked for; the dashboard answers."""
    monkeypatch.setattr(compose, "_get", lambda url, timeout=10: b'{"profile": "core"}')
    monkeypatch.setattr(compose, "wait_until", lambda url, seconds: None)


# --- The image tag (EM C1) ---------------------------------------------------------


def test_the_image_tag_carries_the_profile_and_the_commit():
    assert compose.image_tag("core", COMMIT) == f"showcase-core-{COMMIT[:12]}"
    assert compose.image_tag("full", COMMIT) == f"showcase-full-{COMMIT[:12]}"


@pytest.mark.regression
@pytest.mark.parametrize(
    "tag, why",
    [
        (None, "unset"),
        ("", "unset"),
        ("core", "a developer's own image tag"),
        ("full", "a developer's own image tag"),
        (f"showcase-{COMMIT[:12]}", "not showcase-<profile>-<sha12>"),
        (f"showcase-full-{COMMIT[:12]}", "not this run's"),
        ("showcase-core-0123456789ab", "not this run's"),
    ],
)
def test_an_image_tag_that_is_not_this_runs_is_refused(tag, why):
    with pytest.raises(guard.Refused, match=why):
        compose.refuse_image_tag(tag, profile="core", commit=COMMIT)


@pytest.mark.regression
def test_a_core_image_tagged_under_the_full_runs_name_is_refused_not_reused(
    tmp_path, served
):
    """EM C1 tamper: an image already under the tag is reused only with its profile."""
    fake = Fake(tmp_path)
    tag = f"showcase-full-{COMMIT[:12]}"
    fake.answers = [
        ["compose ls -a", {"out": "[]"}],
        [f"experimently-api:{tag}", inspect(API_ID, "core")],
    ]
    fake.write()
    stack, _work = make_stack(tmp_path, fake, profile="full")
    with pytest.raises(guard.Refused, match="labelled profile 'core', not 'full'"):
        stack.up()
    assert not [s for s in fake.said() if " up " in f" {s} "], fake.said()


# --- Compose's environment (PE C2) ---------------------------------------------------


def test_compose_is_given_the_allow_list_and_nothing_from_the_caller(tmp_path):
    fake = Fake(tmp_path)
    caller = {
        **fake.parent(),
        "COMPOSE_FILE": "/elsewhere/docker-compose.yml",
        "COMPOSE_PROFILES": "demo",
        "SEED": "demo,shoplab",
        "CORS_ORIGINS": "*",
        "SDK_RATE_LIMIT_PER_MINUTE": "60000",
        "AWS_PROFILE": "someone",
        "DOCKER_HOST": "unix:///var/run/docker.sock",
    }
    stack, _ = make_stack(tmp_path, fake)
    env = guard.child_env(stack.settings, parent=caller, docker=True)
    assert sorted(env) == sorted(
        ["PATH", "HOME", "TMPDIR", "DOCKER_HOST", *guard.COMPOSE_KEYS]
    )
    assert env["COMPOSE_PROJECT_NAME"] == "showcase-20261015120000-abc123"
    assert env["EXPERIMENTLY_IMAGE_TAG"] == f"showcase-core-{COMMIT[:12]}"
    assert [env[k] for k in ("POSTGRES_HOST_PORT", "REDIS_HOST_PORT")] == [
        "28402",
        "28405",
    ]
    assert [env[k] for k in ("API_HOST_PORT", "FRONTEND_HOST_PORT")] == [
        "28400",
        "28401",
    ]
    compose.refuse_compose_env(env, (REPO_ROOT / "docker-compose.yml").read_text())


@pytest.mark.regression
@pytest.mark.parametrize(
    "extra, why",
    [
        ({"SEED": "demo,shoplab"}, "holds SEED"),
        ({"COMPOSE_FILE": "x.yml"}, "holds COMPOSE_FILE"),
        ({"AWS_PROFILE": "x"}, "holds AWS_PROFILE"),
    ],
)
def test_a_variable_off_composes_list_is_refused(tmp_path, extra, why):
    fake = Fake(tmp_path)
    stack, _ = make_stack(tmp_path, fake)
    with pytest.raises(guard.Refused, match=why):
        compose.refuse_compose_env(
            {**stack.compose_env, **extra},
            (REPO_ROOT / "docker-compose.yml").read_text(),
        )


def test_an_interpolated_variable_is_refused_even_on_the_list(monkeypatch):
    """A key on the list that the compose file also reads, not the tool's own."""
    monkeypatch.setattr(guard, "PASSED", guard.PASSED + ("SEED",))
    with pytest.raises(guard.Refused, match="sets SEED, which the compose file reads"):
        compose.refuse_compose_env({"SEED": "demo"}, "x: ${SEED-demo}\n")


def test_the_compose_file_reads_the_tools_seven_settings():
    read = compose.interpolated((REPO_ROOT / "docker-compose.yml").read_text())
    assert set(guard.COMPOSE_KEYS) - {"COMPOSE_PROJECT_NAME"} <= set(read)


@pytest.mark.regression
@pytest.mark.parametrize("name", compose.UNASKED_FILES)
def test_a_file_compose_reads_unasked_is_refused_before_up(tmp_path, name, served):
    fake = Fake(tmp_path)
    fake.answers = healthy_answers("showcase-20261015120000-abc123")
    fake.write()
    stack, _ = make_stack(tmp_path, fake)
    (stack.tree / name).write_text("SEED=demo,shoplab\n")
    with pytest.raises(guard.Refused, match=f"{name} is in the recorded tree"):
        stack.up()
    assert fake.said() == []


# --- Up, the published ports (PE C3), the record (PE C4) -----------------------------


def test_up_runs_the_quick_starts_command_and_records_the_images(tmp_path, served):
    fake = Fake(tmp_path)
    project = "showcase-20261015120000-abc123"
    fake.answers = healthy_answers(project)
    fake.write()
    stack, work = make_stack(tmp_path, fake)
    stack.up()
    said = fake.said()
    up = [s for s in said if s.endswith("up -d --wait")]
    assert up == [
        f"compose -p {project} -f {stack.tree / 'docker-compose.yml'} up -d --wait"
    ]
    # The record was written before up, and names the project.
    line = next(c for c in fake.calls() if "records" in c)
    assert line["records"] == [f"{project}.json"]
    assert stack.facts["built"] == ["api", "frontend"]
    assert stack.facts["images"]["api"] == {
        "image": f"experimently-api:showcase-core-{COMMIT[:12]}",
        "id": API_ID,
    }
    assert stack.facts["images"]["frontend"]["id"] == WEB_ID
    manifest = stack.manifest()
    assert manifest["name"] == "quickstart" and manifest["project"] == project
    assert manifest["overrides"] == []
    # Nothing the tool says to docker names --rmi, and compose gets the allow-list.
    assert not [s for s in said if "--rmi" in s]
    for call in fake.calls():
        if "env" in call:
            assert set(call["env"]) <= set(guard.allowed_env_keys("compose")) | {
                "PWD",
                "SHLVL",
                "_",
                "__CF_USER_TEXT_ENCODING",
                "LC_CTYPE",
            }
    contract_manifest = contract.Manifest(
        **_manifest_fields(),
        raw={"stack": manifest | {"image_tag": stack.tag}},
    )
    assert contract.stack_problems(contract_manifest) == []


def _manifest_fields() -> Dict[str, Any]:
    return {
        "ref": "v0.34.2",
        "commit": COMMIT,
        "next_version": "16",
        "lock_next_version": "16",
        "profile": "core",
        "viewport": dict(contract.VIEWPORT),
        "zoom": 4 / 3,
        "page_h": contract.PAGE_H,
        "band_h": contract.BAND_H,
        "voice": contract.SILENT,
        "gates": {},
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "changed, why",
    [
        ({"postgres": [("127.0.0.1", 5432, 5432)]}, "postgres publishes"),
        ({"api": [("0.0.0.0", 28400, 8000)]}, "api publishes"),
        ({"redis": []}, "redis publishes"),
        ({"shoplab": [("127.0.0.1", 3200, 3200)]}, "service shoplab is running"),
        (
            {"frontend": [("127.0.0.1", 28401, 8080), ("127.0.0.1", 28499, 8080)]},
            "frontend publishes",
        ),
    ],
)
def test_a_port_that_is_not_the_tools_refuses_after_up(tmp_path, served, changed, why):
    fake = Fake(tmp_path)
    project = "showcase-20261015120000-abc123"
    answers = healthy_answers(project)
    answers = [a for a in answers if a[0] != "ps --format json"]
    answers.insert(3, ["ps --format json", {"out": ps_lines(project, **changed)}])
    fake.answers = answers
    fake.write()
    stack, _ = make_stack(tmp_path, fake)
    with pytest.raises(StackError, match=f"PE C3: .*{why}"):
        stack.up()


def test_the_published_ports_gate_passes_exactly_the_four():
    found = compose.published(compose.json_lines(ps_lines("showcase-x")))
    assert compose.published_problems(found, PORTS) == []
    # One JSON array, as older compose prints it.
    lines = [json.loads(x) for x in ps_lines("showcase-x").splitlines()]
    assert compose.published(compose.json_lines(json.dumps(lines))) == found


@pytest.mark.regression
def test_a_reserved_redis_port_is_refused_before_any_docker_call(tmp_path):
    """The Redis port left at its default (6379) is refused in preflight."""
    options = run.Options(
        videos=["01-getting-started"],
        ref="v0.34.2",
        repo=REPO_ROOT,
        work_dir=tmp_path / "work",
        profile="core",
        ports=Ports(28400, 28401, 28402, 28403, 28404, 6379),
        out=tmp_path / "videos",
        render=False,
    )
    with pytest.raises(guard.Refused, match="Redis port 6379 is reserved"):
        run.preflight(options, {"HOME": str(tmp_path)})


def test_the_redis_port_is_apart_from_the_apps_ports():
    assert config.DEFAULT_REDIS_PORT not in (
        config.DEFAULT_SHOPLAB_PORT,
        config.DEFAULT_STREAMPULSE_PORT,
    )


@pytest.mark.regression
@pytest.mark.parametrize(
    "name", ["experimently", "experimently-local", "acc905", "Showcase-x"]
)
def test_a_project_that_is_not_the_tools_is_refused(name):
    with pytest.raises(guard.Refused, match="is not one of the tool's"):
        compose.refuse_project(name)


@pytest.mark.regression
def test_a_project_compose_already_lists_is_refused_before_up(tmp_path, served):
    fake = Fake(tmp_path)
    project = "showcase-20261015120000-abc123"
    fake.answers = [
        ["compose ls -a", {"out": json.dumps([{"Name": project, "ConfigFiles": "/x"}])}]
    ]
    fake.write()
    stack, _ = make_stack(tmp_path, fake)
    with pytest.raises(guard.Refused, match="exists already"):
        stack.up()
    assert not [s for s in fake.said() if s.endswith("up -d --wait")]


# --- Down and recovery (EM C3, C4) ----------------------------------------------------


def test_down_is_scoped_to_the_project_and_leaves_nothing(tmp_path, served):
    fake = Fake(tmp_path)
    project = "showcase-20261015120000-abc123"
    fake.answers = healthy_answers(project)
    fake.write()
    stack, work = make_stack(tmp_path, fake)
    stack.up()
    stack.down()
    said = fake.said()
    down = [s for s in said if " down " in f" {s} "]
    assert down == [
        f"compose -p {project} -f {stack.tree / 'docker-compose.yml'}"
        " down -v --remove-orphans --timeout 10"
    ]
    label = f"label={config.COMPOSE_PROJECT_LABEL}={project}"
    assert f"ps -a -q --filter {label}" in said
    assert f"volume ls -q --filter {label}" in said
    assert stack.facts["left"] == {"containers": [], "volumes": [], "networks": []}
    assert not (work.path / compose.PROJECTS_DIR / f"{project}.json").exists()


@pytest.mark.regression
def test_down_refuses_when_something_labelled_is_left(tmp_path, served):
    fake = Fake(tmp_path)
    project = "showcase-20261015120000-abc123"
    fake.answers = healthy_answers(project) + [
        ["volume ls -q --filter", {"out": f"{project}_postgres_data\n"}],
    ]
    fake.write()
    stack, _ = make_stack(tmp_path, fake)
    stack.up()
    with pytest.raises(StackError, match="left after down"):
        stack.down()
    assert f"volume rm -f {project}_postgres_data" in fake.said()


@pytest.mark.regression
def test_recovery_removes_a_killed_runs_project_from_this_work_root(tmp_path):
    fake = Fake(tmp_path)
    work = guard.WorkRoot(tmp_path / "work")
    work.path.mkdir()
    tree = work.path / "tree-core"
    tree.mkdir()
    files = [tree / "docker-compose.yml"]
    files[0].write_text("services: {}\n")
    project = "showcase-20261015110000-dead01"
    compose.record_project(work, project, files)
    fake.answers = [
        [
            "compose ls -a",
            {"out": json.dumps([{"Name": project, "ConfigFiles": str(files[0])}])},
        ],
        ["ps -a -q --filter", {"out": "c1\n", "once": True}],
        ["volume ls -q --filter", {"out": f"{project}_postgres_data\n", "once": True}],
    ]
    fake.write()
    docker = compose.Docker(
        Children(work, fake.parent()), fake.parent(), work.path / "logs"
    )
    removed = compose.recover(docker, work)
    assert removed == [project]
    said = fake.said()
    assert (
        f"compose -p {project} -f {files[0]} down -v --remove-orphans --timeout 10"
        in said
    )
    assert "rm -f -v c1" in said
    assert f"volume rm -f {project}_postgres_data" in said
    assert not list((work.path / compose.PROJECTS_DIR).glob("*.json"))


@pytest.mark.regression
def test_recovery_refuses_a_project_started_from_outside_the_work_root(tmp_path):
    fake = Fake(tmp_path)
    work = guard.WorkRoot(tmp_path / "work")
    work.path.mkdir()
    project = "showcase-20261015110000-dead01"
    compose.record_project(work, project, [work.path / "docker-compose.yml"])
    fake.answers = [
        [
            "compose ls -a",
            {
                "out": json.dumps(
                    [
                        {
                            "Name": project,
                            "ConfigFiles": "/Users/someone/experimently/docker-compose.yml",
                        }
                    ]
                )
            },
        ]
    ]
    fake.write()
    docker = compose.Docker(
        Children(work, fake.parent()), fake.parent(), work.path / "logs"
    )
    with pytest.raises(guard.Refused, match="not from this work root"):
        compose.recover(docker, work)
    assert not [s for s in fake.said() if "down" in s or " rm " in f" {s} "]


@pytest.mark.regression
def test_a_record_naming_a_developers_project_is_refused(tmp_path):
    fake = Fake(tmp_path)
    fake.answers = [["compose ls -a", {"out": "[]"}]]
    fake.write()
    work = guard.WorkRoot(tmp_path / "work")
    folder = work.path / compose.PROJECTS_DIR
    folder.mkdir(parents=True)
    (folder / "experimently.json").write_text(
        json.dumps({"project": "experimently", "files": []})
    )
    docker = compose.Docker(
        Children(work, fake.parent()), fake.parent(), work.path / "logs"
    )
    with pytest.raises(guard.Refused, match="is not one of the tool's"):
        compose.recover(docker, work)
    assert fake.said() == ["compose ls -a --format json"]


# --- The override list (PE C5) --------------------------------------------------------


def board(slug: str) -> storyboard.Storyboard:
    path = run.board_path(slug)
    return storyboard.load(path)


def test_every_video_and_probe_has_a_checked_override_list():
    listed = override_lists.load()
    for slug in list(config.VIDEOS) + list(config.PROBES):
        made = override_lists.override_list(board(slug), PORTS, listed)
        if slug in config.VIDEOS:
            assert made == [], slug
    probe = override_lists.override_list(board("90-app-probe"), PORTS, listed)
    assert probe == [
        compose.Override(
            "CORS_ORIGINS",
            "http://127.0.0.1:28401,http://127.0.0.1:28403,http://127.0.0.1:28404",
        )
    ]


@pytest.mark.regression
@pytest.mark.parametrize(
    "entries, why",
    [
        (None, "no entry for 90-app-probe"),
        ({}, "needs CORS_ORIGINS"),
        ({"SEED": {"value": "demo", "why": "x" * 50}}, "needs CORS_ORIGINS"),
        (
            {"SDK_RATE_LIMIT_PER_MINUTE": {"value": "60000", "why": "x" * 50}},
            "may not be set by a video",
        ),
        (
            {"WAREHOUSE_CREDENTIALS_KEYS": {"value": "k", "why": "x" * 50}},
            "may not be set by a video",
        ),
        ({"API_HOST_PORT": {"value": "1", "why": "x" * 50}}, "may not be set"),
        ({"COMPOSE_FILE": {"value": "x", "why": "x" * 50}}, "may not be set"),
        ({"CORS_ORIGINS": {"value": "{dashboard}"}}, "exactly one of why or statement"),
        ({"CORS_ORIGINS": {"value": "{dashboard}", "why": "short"}}, "is not a reason"),
        (
            {
                "CORS_ORIGINS": {
                    "value": "{dashboard}",
                    "statement": "never said on screen",
                }
            },
            "no scene states",
        ),
        (
            {"CORS_ORIGINS": {"value": "{dashboard},{shoplab}", "why": "x" * 50}},
            "not the dashboard and the video's apps",
        ),
        ({"CORS_ORIGINS": {"value": "*", "why": "x" * 50}}, "not the dashboard"),
    ],
)
def test_an_override_list_that_is_not_justified_is_refused(entries, why):
    listed = {} if entries is None else {"90-app-probe": entries}
    with pytest.raises(guard.Refused, match=why):
        override_lists.override_list(board("90-app-probe"), PORTS, listed)


def test_a_stated_override_names_words_on_screen():
    probe = board("90-app-probe")
    said = probe.scenes[0].departures[0].says
    made = override_lists.override_list(
        probe,
        PORTS,
        {
            "90-app-probe": {
                "CORS_ORIGINS": {
                    "value": "{dashboard},{shoplab},{streampulse}",
                    "statement": said,
                }
            }
        },
    )
    assert [m.key for m in made] == ["CORS_ORIGINS"]


@pytest.mark.regression
def test_preflight_refuses_an_undeclared_key_before_any_docker_call(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(override_lists, "load", lambda path=None: {"90-app-probe": {}})
    options = run.Options(
        videos=["90-app-probe"],
        ref="v0.34.2",
        repo=REPO_ROOT,
        work_dir=tmp_path / "work",
        profile="core",
        ports=PORTS,
        out=tmp_path / "videos",
        render=False,
    )
    with pytest.raises(guard.Refused, match="needs CORS_ORIGINS"):
        run.preflight(options, {"HOME": str(tmp_path)})


def test_an_override_reaches_the_api_through_a_file_not_composes_environment(
    tmp_path, served
):
    fake = Fake(tmp_path)
    project = "showcase-20261015120000-abc123"
    fake.answers = healthy_answers(project)
    fake.write()
    item = compose.Override("CORS_ORIGINS", "http://127.0.0.1:28401")
    stack, _ = make_stack(tmp_path, fake, overrides=[item])
    # As in a run: nothing has made the video's logs directory yet (Mac run 1).
    stack.logs.rmdir()
    stack.up()
    override = stack.logs / "compose.override.yml"
    import yaml

    assert yaml.safe_load(override.read_text()) == {
        "services": {"api": {"environment": {"CORS_ORIGINS": "http://127.0.0.1:28401"}}}
    }
    up = next(s for s in fake.said() if s.endswith("up -d --wait"))
    assert up.endswith(f"-f {override} up -d --wait")
    assert "CORS_ORIGINS" not in stack.compose_env
    assert stack.manifest()["overrides"] == ["CORS_ORIGINS"]


# --- The server key, into memory only (PE C7) -----------------------------------------


@pytest.mark.regression
def test_the_server_key_is_read_from_the_api_container_into_memory_only(
    tmp_path, served
):
    fake = Fake(tmp_path)
    project = "showcase-20261015120000-abc123"
    fake.answers = healthy_answers(project) + [
        ["streampulse/.api_key_server", {"out": SERVER_KEY + "\n"}],
        ["streampulse/.api_key", {"out": config.APP_STATIC_KEY["streampulse"] + "\n"}],
    ]
    fake.write()
    stack, work = make_stack(tmp_path, fake)
    stack.up()
    assert stack.seed_app("streampulse", history=False) == SERVER_KEY
    said = fake.said()
    tree_file = stack.tree / "docker-compose.yml"
    assert (
        f"compose -p {project} -f {tree_file} exec -T api python -m"
        " backend.scripts.seed_streampulse --no-history"
    ) in said
    for path in work.path.rglob("*"):
        if path.is_file():
            assert SERVER_KEY not in path.read_text(errors="replace"), path
    assert not [s for s in said if SERVER_KEY in s]


@pytest.mark.regression
@pytest.mark.parametrize("text", ["", "\n", "two keys\n", "a\tb"])
def test_a_key_file_that_is_not_one_key_is_refused(text):
    with pytest.raises(StackError, match="does not hold one key"):
        compose.key_text(text, ".api_key_server")


def test_the_key_read_with_its_newline_is_kept_without_it():
    assert compose.key_text(SERVER_KEY + "\n", ".api_key_server") == SERVER_KEY


@pytest.mark.regression
def test_a_seed_that_registered_another_key_is_refused(tmp_path, served):
    fake = Fake(tmp_path)
    fake.answers = healthy_answers("showcase-20261015120000-abc123") + [
        ["shoplab/.api_key", {"out": "eptk_someone_elses_key\n"}],
    ]
    fake.write()
    stack, _ = make_stack(tmp_path, fake)
    stack.up()
    with pytest.raises(StackError, match="not the compose key"):
        stack.seed_app("shoplab", history=True)


# --- Final recordings: quickstart only (EM C5) ---------------------------------------


@pytest.mark.regression
def test_a_render_of_the_uvicorn_stack_is_refused_at_preflight(tmp_path):
    options = run.Options(
        videos=["01-getting-started"],
        ref="v0.34.2",
        repo=REPO_ROOT,
        work_dir=tmp_path / "work",
        profile="core",
        ports=PORTS,
        out=tmp_path / "videos",
        render=True,
        stack="uvicorn",
    )
    with pytest.raises(guard.Refused, match="rendered only from the quickstart stack"):
        run.preflight(options, {"HOME": str(tmp_path)})


@pytest.fixture
def plenty_of_disk(monkeypatch):
    monkeypatch.setattr(pipeline, "check_disk", lambda paths, floor: {})


@pytest.mark.regression
@pytest.mark.parametrize(
    "change, why",
    [
        (lambda m: m["stack"].update(name="uvicorn"), "made on the 'uvicorn' stack"),
        (lambda m: m.pop("stack"), "names no stack"),
        (
            lambda m: m["stack"].update(image_tag="core"),
            "stack image tag 'core' is not",
        ),
        (
            lambda m: m["stack"]["images"]["api"].update(id=None),
            "no api image ID",
        ),
        (lambda m: m["stack"].update(project="experimently"), "is not showcase-"),
    ],
)
def test_the_render_refuses_a_capture_not_made_on_the_quickstart_stack(
    tmp_path, plenty_of_disk, change, why
):
    capture = make_capture(tmp_path / "capture")
    edit_json(capture / "manifest.json", change)
    with pytest.raises(Refused) as caught:
        pipeline.render(capture, tmp_path / "out")
    assert caught.value.gate == "stack" and why in str(caught.value)


def test_the_review_page_names_the_stack_and_its_images():
    from showcase.render import review

    rows = dict(review.stack_facts(make_stack_manifest()))
    assert rows["Stack"] == "quickstart"
    assert rows["Image tag"] == f"showcase-core-{COMMIT[:12]}"
    assert API_ID in rows["API image"] and WEB_ID in rows["Dashboard image"]


def make_stack_manifest() -> Dict[str, Any]:
    tag = f"showcase-core-{COMMIT[:12]}"
    return {
        "name": "quickstart",
        "project": "showcase-x",
        "image_tag": tag,
        "images": {
            "tag": tag,
            "api": {"image": f"experimently-api:{tag}", "id": API_ID},
            "frontend": {"image": f"experimently-web:{tag}", "id": WEB_ID},
        },
    }


# --- V2's traffic at the default limit (EM C8) ----------------------------------------


class FakeClock:
    """Time that moves only when the sender sleeps."""

    def __init__(self):
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += max(seconds, 0.0)


class Answer:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body or {}
        self.headers = headers or {}

    def json(self):
        return self._body


class LimitedPlatform:
    """The API's SDK limit, on the fake clock: 100 requests in any second, then 429."""

    def __init__(self, clock: FakeClock, per_second: int = 100):
        self.clock = clock
        self.per_second = per_second
        self.times: List[float] = []
        self.answered_429 = 0

    def session(self):
        return self

    def post(self, url, json=None, headers=None, timeout=None):
        now = self.clock()
        self.times = [t for t in self.times if t > now - 1.0]
        if len(self.times) >= self.per_second:
            self.answered_429 += 1
            return Answer(429, headers={"Retry-After": "1"})
        self.times.append(now)
        if url.endswith("/assign"):
            return Answer(
                200, {"is_control": True, "assigned": True, "variant_name": "Control"}
            )
        return Answer(200, {"id": "e"})


def tiny_plan(users: int = 300) -> traffic.Plan:
    people = [
        type("U", (), {"user_id": f"u{i}", "variant_name": "control"})()
        for i in range(users)
    ]
    return traffic.Plan(
        key="k",
        event="purchase",
        users=people,
        events_for=lambda user, arm: [],
        split={},
        p_value=None,
    )


def test_paced_traffic_stays_under_the_default_limit_and_says_how_long_it_took():
    clock = FakeClock()
    platform = LimitedPlatform(clock)
    sender = traffic.Sender(
        tiny_plan(),
        api_url="http://127.0.0.1:28400",
        api_key="made-up-traffic-key-0123456789",
        workers=1,
        session=platform.session,
        rate=config.TRAFFIC_REQUESTS_PER_SECOND,
        sleep=clock.sleep,
        clock=clock,
    )
    sender.start()
    report = sender.finish(timeout=60)
    numbers = report.as_numbers()
    assert numbers["rate_limited"] == 0 and numbers["requests"] == 300
    assert numbers["wall_seconds"] == pytest.approx(299 / 90, abs=0.05)
    assert numbers["requests_per_second"] == 90.0


@pytest.mark.regression
def test_traffic_faster_than_the_default_limit_fails_on_its_429s():
    """EM C8 tamper: paced over 100 a second, the API answers 429 and the take is refused."""
    clock = FakeClock()
    platform = LimitedPlatform(clock)
    sender = traffic.Sender(
        tiny_plan(),
        api_url="http://127.0.0.1:28400",
        api_key="made-up-traffic-key-0123456789",
        workers=1,
        session=platform.session,
        rate=150.0,
        sleep=clock.sleep,
        clock=clock,
    )
    sender.start()
    with pytest.raises(traffic.TrafficFailed, match="answered 429") as raised:
        sender.finish(timeout=60)
    assert platform.answered_429 > 0
    assert "made-up-traffic-key" not in str(raised.value)


def test_a_429_is_honoured_and_sent_again_then_counted():
    clock = FakeClock()
    platform = LimitedPlatform(clock, per_second=10_000)
    first = [True]
    post = platform.post

    def once_429(url, **kw):
        if first[0]:
            first[0] = False
            return Answer(429, headers={"Retry-After": "3"})
        return post(url, **kw)

    platform.post = once_429
    sender = traffic.Sender(
        tiny_plan(5),
        api_url="http://127.0.0.1:28400",
        api_key="made-up-traffic-key-0123456789",
        workers=1,
        session=platform.session,
        rate=90.0,
        sleep=clock.sleep,
        clock=clock,
    )
    sender.start()
    with pytest.raises(
        traffic.TrafficFailed, match="EM C8: the API answered 429 1 times"
    ):
        sender.finish(timeout=60)
    assert sender.report.sent == 5 and sender.report.requests == 6
    assert clock.now >= 3.0


def test_the_quick_start_stack_keeps_the_default_sdk_limit():
    assert (
        config.TRAFFIC_REQUESTS_PER_SECOND
        < config.DEFAULT_SDK_RATE_LIMIT_PER_MINUTE / 60
    )


def test_the_card_after_the_traffic_says_the_measured_time():
    v2 = board("02-first-experiment")
    later = next(s for s in v2.scenes if s.traffic == "finish")
    report = traffic.Report(wall_seconds=741.0)
    filled = run.with_traffic_minutes(later, report)
    assert "sent by the tool in 12 minutes (measured)" in filled.card.note
    assert run.TRAFFIC_MINUTES not in filled.card.note
    assert run.minutes_text(20) == "1 minute"


@pytest.mark.regression
def test_the_measured_time_before_the_traffic_finished_is_refused():
    v2 = board("02-first-experiment")
    later = next(s for s in v2.scenes if s.traffic == "finish")
    early = later.model_copy(update={"traffic": None})
    with pytest.raises(StackError, match="before the traffic finished"):
        run.with_traffic_minutes(early, traffic.Report())


# --- The director: a 429 on a page request, and a page closed in the drain ----------


class Request:
    def __init__(self, url):
        self.url = url
        self.headers = {}
        self.failure = None
        self.post_data_json = None


class Response:
    def __init__(self, url, status, request):
        self.url = url
        self.status = status
        self.request = request
        self.headers = {"content-type": "text/html"}


class Page:
    def __init__(self):
        self.handlers: Dict[str, Any] = {}
        self.closed = False

    def on(self, event, handler):
        self.handlers.setdefault(event, []).append(handler)

    def fire(self, event, value):
        for handler in self.handlers.get(event, []):
            handler(value)

    def wait_for_timeout(self, ms):
        if self.closed:
            raise director.PlaywrightError(
                "Target page, context or browser has been closed"
            )


class Context(Page):
    def route(self, *args):
        pass

    def route_web_socket(self, *args):
        pass

    def add_init_script(self, script):
        pass


def make_director(tmp_path):
    page, context = Page(), Context()
    made = director.Director(
        context=context,
        page=page,
        frames=tmp_path / "work" / "v" / "capture" / "frames",
        dashboard="http://127.0.0.1:28401",
        api_port=28400,
        dashboard_port=28401,
        redactor=director.redaction.Redactor(),
        forbidden=(),
        work=guard.WorkRoot(tmp_path / "work"),
        apps={},
    )
    made.install()
    made.where = "scene arriving beat 2"
    return made, page


@pytest.mark.regression
def test_a_429_on_a_page_request_is_counted_even_off_camera(tmp_path):
    made, page = make_director(tmp_path)
    url = "http://127.0.0.1:28401/api/v1/results/x"
    request = Request(url)
    page.fire("request", request)
    page.fire("response", Response(url, 429, request))
    assert made.tally.rate_limited == [
        "429 from /api/v1/results/x (scene arriving beat 2)"
    ]


@pytest.mark.regression
def test_a_page_closed_while_the_drain_waits_is_a_capture_failure(tmp_path):
    """E0 follow-up 5: Playwright's own error escaped the drain."""
    made, page = make_director(tmp_path)
    made.counting = "dashboard"
    made.tally.apps["dashboard"] = {"answers_400": [], "failed": [], "unanswered": []}
    page.fire("request", Request("http://127.0.0.1:28401/api/v1/experiments"))
    page.closed = True
    with pytest.raises(
        director.CaptureFailed, match="the page went while 1 requests were out"
    ):
        made.drain("dashboard")


# --- Children.capture --------------------------------------------------------------


def test_a_private_read_is_not_logged(tmp_path):
    fake = Fake(tmp_path)
    fake.answers = [["cat", {"out": SERVER_KEY + "\n"}]]
    fake.write()
    work = guard.WorkRoot(tmp_path / "work")
    work.path.mkdir()
    children = Children(work, fake.parent())
    log = work.path / "read.log"
    status, out = children.capture(
        "read-key",
        ["docker", "cat", "x"],
        cwd=work.path,
        env=guard.child_env({}, parent=fake.parent(), docker=True),
        log=log,
        kind="compose",
        private=True,
    )
    assert status == 0 and out.strip() == SERVER_KEY
    assert SERVER_KEY not in log.read_text() and "held in memory" in log.read_text()
