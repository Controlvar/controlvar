"""The Quick Start stack: ``docker compose up -d --wait`` in the recorded tree.

``--stack quickstart`` (the default, and the only stack a video is rendered
from) records against what a new user starts with the Quick Start's own
command (docs/getting-started/quick-start.md), in ``git archive <ref>``:
the root ``docker-compose.yml`` builds the API and dashboard images, starts
Postgres 16 and Redis 7, bootstraps the schema and applies the ``demo`` seed.
What the tool changes, and why, is all here:

* **The project** is ``showcase-<run id>`` (``project_name``), never a name a
  developer's own stack uses; ``docker compose ls -a`` must not show it yet
  (``refuse_project_listed``). Its name and compose files are written into the
  work root *before* ``up`` (``record_project``), so a killed run's project is
  found and removed by the next run (``recover``): ``down -v`` on that project
  only, and only when every compose file it was started from is inside this
  work root. Then what compose labelled with the project and is still there
  (containers, volumes, networks) is removed by that label, and nothing may be
  left.
* **The images** are tagged ``showcase-<profile>-<sha12>``
  (``EXPERIMENTLY_IMAGE_TAG``, ``image_tag``): a developer's own
  ``experimently-api:core`` is neither recorded nor overwritten. An image
  already under the tag is reused only when its ``io.experimently.profile``
  label is the profile asked for (``refuse_wrong_profile_image``); a core
  image tagged under the full run's name is refused, not reused. The images
  are never removed (no ``--rmi``).
* **The environment** compose runs with is an allow-list (``compose_settings``,
  child kind ``compose``): the project name, the profile, the image tag and
  the four ``*_HOST_PORT`` variables, on top of ``PATH``, ``HOME``, ``TMPDIR``
  and Docker's own client settings. No other variable the compose file reads
  (``interpolated``) may be in it, nor any other ``COMPOSE_*``; a ``.env`` or
  a compose override file in the tree is refused (compose reads both without
  being asked).
* **Settings a video needs** beyond those (the demo apps' origins in
  ``CORS_ORIGINS``) come from ``overrides.yaml``, one list per video, each key
  justified in writing there or stated on screen (``overrides``); they reach
  the API through an override file in the video's work directory, never
  through compose's environment.
* **After up**, ``docker compose ps`` must publish exactly the tool's four
  ports on 127.0.0.1, one per service (``published_problems``), the API must
  report the profile asked for, and the images the containers run are the
  tagged ones, whose IDs the manifest records.

The demo apps a video records are E0's own build (``build.build_app``),
served by ``next start`` as on the uvicorn stack; their seeds run in the API
container (``docker compose exec -T api python -m backend.scripts.seed_<app>``),
which writes their key files to the ``demo_keys`` volume. The StreamPulse
server key is read from there by ``exec -T api cat``, into this process only
(``Children.capture``): never logged, written to a file, or put on an argv.
"""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from showcase.capture import build, config, guard
from showcase.capture.procs import Children
from showcase.capture.stack import Ports, StackError, _get, wait_until

#: The compose file the Quick Start runs, at the tree's root.
COMPOSE_FILE = "docker-compose.yml"
#: What compose reads from the project directory without being asked.
UNASKED_FILES = (
    ".env",
    "compose.override.yml",
    "compose.override.yaml",
    "docker-compose.override.yml",
    "docker-compose.override.yaml",
)
#: A project the tool may start, and may later remove.
PROJECT = re.compile(r"^showcase-[a-z0-9][a-z0-9-]{0,60}$")
#: Where each run's project is written before ``up``: ``<work root>/<this>/<project>.json``.
PROJECTS_DIR = "compose-projects"
#: The services the Quick Start starts, and the port each publishes inside.
SERVICES: Dict[str, int] = {
    "postgres": 5432,
    "redis": 6379,
    "api": 8000,
    "frontend": 8080,
}
#: Each service's image, by the compose file's names (``x-api-image``, ``x-web-image``).
IMAGES = {"api": "experimently-api", "frontend": "experimently-web"}
#: The image tag: the profile and the recorded commit's first 12 characters.
IMAGE_TAG = re.compile(r"^showcase-(core|full)-([0-9a-f]{12})$")
#: The variables the tool itself gives compose.
TOOL_KEYS: Tuple[str, ...] = guard.COMPOSE_KEYS
#: ``up --wait`` builds the images the first time: minutes (PE 5 of T166).
UP_TIMEOUT_S = 2400
DOWN_TIMEOUT_S = 300
#: Where the API container's seeds write the apps' key files (the demo_keys volume).
KEY_DIR_IN_API = "/data/demo"


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def project_name(run_id: str) -> str:
    name = f"showcase-{run_id}".lower()
    refuse_project(name)
    return name


def refuse_project(name: str) -> None:
    """Refused unless *name* is one of the tool's own (``showcase-...``)."""
    if not PROJECT.match(name or ""):
        raise guard.Refused(
            f"compose project {name!r} is not one of the tool's (showcase-...);"
            " a developer's own project is never started or removed"
        )


def image_tag(profile: str, commit: str) -> str:
    """``showcase-<profile>-<the commit's first 12 characters>``."""
    tag = f"showcase-{profile}-{commit[:12]}"
    refuse_image_tag(tag, profile=profile, commit=commit)
    return tag


def refuse_image_tag(tag: Optional[str], *, profile: str, commit: str) -> None:
    """EM C1: unset, ``core``, ``full`` or another run's tag are refused."""
    if not tag:
        raise guard.Refused("EXPERIMENTLY_IMAGE_TAG is unset: compose would tag :core")
    if tag in ("core", "full"):
        raise guard.Refused(
            f"EXPERIMENTLY_IMAGE_TAG={tag} is a developer's own image tag"
        )
    found = IMAGE_TAG.match(tag)
    if not found:
        raise guard.Refused(f"image tag {tag!r} is not showcase-<profile>-<sha12>")
    if found.group(1) != profile or not commit.startswith(found.group(2)):
        raise guard.Refused(
            f"image tag {tag!r} is not this run's (profile {profile},"
            f" commit {commit[:12]})"
        )


# ---------------------------------------------------------------------------
# Compose's environment
# ---------------------------------------------------------------------------


def compose_settings(
    *, project: str, profile: str, tag: str, ports: Ports
) -> Dict[str, str]:
    """What the tool gives compose: these seven, and nothing else of its own."""
    return {
        "COMPOSE_PROJECT_NAME": project,
        "EXPERIMENTLY_PROFILE": profile,
        "EXPERIMENTLY_IMAGE_TAG": tag,
        "POSTGRES_HOST_PORT": str(ports.postgres),
        "REDIS_HOST_PORT": str(ports.redis),
        "API_HOST_PORT": str(ports.api),
        "FRONTEND_HOST_PORT": str(ports.dashboard),
    }


_INTERPOLATED = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)")


def interpolated(compose_text: str) -> List[str]:
    """Every variable the compose file reads (``${NAME...}``), sorted."""
    return sorted(set(_INTERPOLATED.findall(compose_text)))


def refuse_compose_env(env: Mapping[str, str], compose_text: str) -> None:
    """PE C2: compose's environment holds the allow-list and nothing more.

    Refused: any key off the ``compose`` kind's list, any ``COMPOSE_*`` but
    the project name, and any variable the compose file reads that is not one
    the tool gives it.
    """
    allowed = guard.allowed_env_keys("compose")
    extra = sorted(set(env) - allowed)
    if extra:
        raise guard.Refused(f"compose's environment holds {', '.join(extra)}")
    for key in sorted(env):
        if key.startswith("COMPOSE_") and key != "COMPOSE_PROJECT_NAME":
            raise guard.Refused(f"compose's environment holds {key}")
    read = set(interpolated(compose_text))
    foreign = sorted((read & set(env)) - set(TOOL_KEYS))
    if foreign:
        raise guard.Refused(
            f"compose's environment sets {', '.join(foreign)}, which the compose"
            " file reads; only the tool's own settings may"
        )


def refuse_unasked_files(tree: Path) -> None:
    """A ``.env`` or a compose override file in the tree changes the stack unasked."""
    for name in UNASKED_FILES:
        if (tree / name).exists():
            raise guard.Refused(
                f"{name} is in the recorded tree; compose would read it without"
                " being asked"
            )


# ---------------------------------------------------------------------------
# Docker's answers, parsed
# ---------------------------------------------------------------------------


def json_lines(text: str) -> List[Dict[str, Any]]:
    """``--format json``: one array (older compose) or one object a line."""
    text = text.strip()
    if not text:
        return []
    if text.startswith("["):
        data = json.loads(text)
        return [item for item in data if isinstance(item, dict)]
    return [json.loads(line) for line in text.splitlines() if line.strip()]


def published(ps: Sequence[Mapping[str, Any]]) -> Dict[str, List[Tuple[str, int]]]:
    """``docker compose ps --format json``: each service's published (address, port)."""
    found: Dict[str, List[Tuple[str, int]]] = {}
    for entry in ps:
        service = str(entry.get("Service"))
        found.setdefault(service, [])
        for publisher in entry.get("Publishers") or []:
            port = int(publisher.get("PublishedPort") or 0)
            if port:
                found[service].append((str(publisher.get("URL")), port))
    return found


def wanted_ports(ports: Ports) -> Dict[str, List[Tuple[str, int]]]:
    return {
        "postgres": [("127.0.0.1", ports.postgres)],
        "redis": [("127.0.0.1", ports.redis)],
        "api": [("127.0.0.1", ports.api)],
        "frontend": [("127.0.0.1", ports.dashboard)],
    }


def published_problems(
    found: Mapping[str, Sequence[Tuple[str, int]]], ports: Ports
) -> List[str]:
    """PE C3: exactly the tool's four ports, on 127.0.0.1, one per service."""
    wanted = wanted_ports(ports)
    problems = []
    for service in sorted(set(found) | set(wanted)):
        got = sorted(set(found.get(service, [])))
        want = wanted.get(service)
        if want is None:
            problems.append(f"service {service} is running ({got or 'no ports'})")
        elif got != want:
            problems.append(f"{service} publishes {got}, not {want}")
    return problems


def under(root: Path, path: Path) -> bool:
    """True when *path* is inside *root* (both resolved), and is not *root*."""
    where = Path(os.path.realpath(root))
    real = Path(os.path.realpath(path))
    return real != where and where in real.parents


def config_files_inside(listed: str, root: Path) -> bool:
    """``docker compose ls``'s ``ConfigFiles``: every one under *root*."""
    files = [part for part in (listed or "").split(",") if part]
    return bool(files) and all(under(root, Path(f)) for f in files)


# ---------------------------------------------------------------------------
# The project record and recovery
# ---------------------------------------------------------------------------


def record_project(work: guard.WorkRoot, project: str, files: Sequence[Path]) -> Path:
    """Written before ``up``: the project's name and its compose files."""
    refuse_project(project)
    for path in files:
        if not work.contains(path):
            raise guard.Refused(f"{path} is not under the work root {work.path}")
    folder = work.path / PROJECTS_DIR
    folder.mkdir(exist_ok=True)
    record = folder / f"{project}.json"
    record.write_text(
        json.dumps({"project": project, "files": [str(f) for f in files]}),
        encoding="utf-8",
    )
    return record


def recorded_projects(work: guard.WorkRoot) -> List[Dict[str, Any]]:
    """Each project a run of this work root recorded and did not remove."""
    folder = work.path / PROJECTS_DIR
    found = []
    for record in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        try:
            data = json.loads(record.read_text(encoding="utf-8"))
        except ValueError:
            raise guard.Refused(f"{record} is not a project record") from None
        data["record"] = record
        found.append(data)
    return found


class Docker:
    """The docker and compose calls, each through ``Children`` (kind ``compose``)."""

    def __init__(self, children: Children, env: Mapping[str, str], logs: Path):
        self.children = children
        self.env = dict(env)
        self.logs = logs

    def cmd(self, name: str, argv: Sequence[str], *, timeout: float = 300) -> int:
        return self.children.run(
            name,
            list(argv),
            cwd=self.children.work.path,
            env=self.env,
            log=self.logs / f"{name}.log",
            kind="compose",
            timeout=timeout,
        )

    def out(self, name: str, argv: Sequence[str], *, private: bool = False) -> str:
        """The command's standard output, into this process only."""
        status, stdout = self.children.capture(
            name,
            list(argv),
            cwd=self.children.work.path,
            env=self.env,
            log=self.logs / f"{name}.log",
            kind="compose",
            private=private,
        )
        if status != 0:
            raise StackError(f"{name} exited {status}; see {self.logs / name}.log")
        return stdout

    def projects(self) -> Dict[str, str]:
        """``docker compose ls -a``: each project's name and its config files."""
        listed = json_lines(
            self.out(
                "compose-ls", ["docker", "compose", "ls", "-a", "--format", "json"]
            )
        )
        return {str(p.get("Name")): str(p.get("ConfigFiles") or "") for p in listed}

    def labelled(self, project: str) -> Dict[str, List[str]]:
        """What compose made for *project* and is still there, by its label."""
        label = f"label={config.COMPOSE_PROJECT_LABEL}={project}"
        found = {}
        for what, argv in (
            ("containers", ["docker", "ps", "-a", "-q", "--filter", label]),
            ("volumes", ["docker", "volume", "ls", "-q", "--filter", label]),
            ("networks", ["docker", "network", "ls", "-q", "--filter", label]),
        ):
            found[what] = self.out(f"list-{what}", argv).split()
        return found

    def image_labels(self, image: str) -> Optional[Dict[str, Any]]:
        """The image's ID and labels, or None when there is no such image."""
        status, stdout = self.children.capture(
            "image-inspect",
            ["docker", "image", "inspect", "--format", "{{json .}}", image],
            cwd=self.children.work.path,
            env=self.env,
            log=self.logs / "image-inspect.log",
            kind="compose",
        )
        if status != 0:
            return None
        data = json.loads(stdout)
        return {
            "id": data.get("Id"),
            "labels": (data.get("Config") or {}).get("Labels") or {},
        }


def remove_project(
    docker: Docker, project: str, files: Sequence[Path], work: guard.WorkRoot
) -> Dict[str, List[str]]:
    """``down -v`` on *project*, then what still carries its label; what is left.

    Refused for a name that is not the tool's, or a compose file outside the
    work root. Never ``--rmi``: the images stay (EM C3 of T166).
    """
    refuse_project(project)
    for path in files:
        if not work.contains(path):
            raise guard.Refused(f"{path} is not under the work root {work.path}")
    present = [f for f in files if f.is_file()]
    if present:
        argv = ["docker", "compose", "-p", project]
        for path in present:
            argv += ["-f", str(path)]
        docker.cmd(
            "compose-down",
            argv + ["down", "-v", "--remove-orphans", "--timeout", "10"],
            timeout=DOWN_TIMEOUT_S,
        )
    left = docker.labelled(project)
    for what, rm in (
        ("containers", ["docker", "rm", "-f", "-v"]),
        ("volumes", ["docker", "volume", "rm", "-f"]),
        ("networks", ["docker", "network", "rm"]),
    ):
        if left[what]:
            docker.cmd(f"rm-{what}", rm + left[what])
    return docker.labelled(project)


def recover(docker: Docker, work: guard.WorkRoot, log=lambda line: None) -> List[str]:
    """Remove each project a killed run of this work root left (EM C4).

    Only a ``showcase-*`` project recorded under this work root, and only when
    ``docker compose ls`` shows it started from compose files inside the work
    root (or no longer shows it). Refused otherwise: someone else's project.
    """
    removed = []
    listed = docker.projects()
    for entry in recorded_projects(work):
        project = str(entry.get("project"))
        refuse_project(project)
        files = [Path(f) for f in entry.get("files") or []]
        if project in listed and not config_files_inside(listed[project], work.path):
            raise guard.Refused(
                f"compose project {project} runs from {listed[project]}, not from"
                f" this work root {work.path}; it is not removed"
            )
        left = remove_project(docker, project, files, work)
        if any(left.values()):
            raise StackError(f"{project}: left after removal: {left}")
        log(f"removed compose project {project}, which a killed run left")
        work.remove(entry["record"])
        removed.append(project)
    return removed


# ---------------------------------------------------------------------------
# The stack
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Override:
    key: str
    value: str


class QuickstartStack:
    """One video's Quick Start stack; the same face as ``stack.Stack``."""

    name = "quickstart"

    def __init__(
        self,
        *,
        tree: Path,
        ports: Ports,
        profile: str,
        commit: str,
        run_id: str,
        children: Children,
        logs: Path,
        parent: Mapping[str, str],
        apps: Optional[Mapping[str, Path]] = None,
        overrides: Sequence[Override] = (),
    ):
        self.tree = tree
        self.ports = ports
        self.profile = profile
        self.commit = commit
        self.children = children
        self.logs = logs
        self.parent = dict(parent)
        self.apps: Dict[str, Path] = dict(apps or {})
        self.overrides = list(overrides)
        self.project = project_name(run_id)
        self.tag = image_tag(profile, commit)
        self.settings = compose_settings(
            project=self.project, profile=profile, tag=self.tag, ports=ports
        )
        self.compose_env = guard.child_env(self.settings, parent=parent, docker=True)
        self.docker_env = self.compose_env
        self.docker = Docker(children, self.compose_env, logs)
        self.app_envs = {
            app: guard.child_env(
                guard.app_settings(
                    app=app, api_port=ports.api, dashboard_port=ports.dashboard
                ),
                parent=parent,
            )
            for app in self.apps
        }
        self.files: List[Path] = [tree / COMPOSE_FILE]
        self.record: Optional[Path] = None
        self.facts: Dict[str, Any] = {}

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.ports.api}"

    @property
    def dashboard_url(self) -> str:
        return f"http://127.0.0.1:{self.ports.dashboard}"

    def app_url(self, app: str) -> str:
        return guard.origin(self.ports.app(app))

    def _compose(self, *args: str) -> List[str]:
        argv = ["docker", "compose", "-p", self.project]
        for path in self.files:
            argv += ["-f", str(path)]
        return argv + list(args)

    # -- before up ---------------------------------------------------------------

    def write_override(self, path: Path) -> None:
        """The API's settings this video needs beyond the Quick Start's."""
        if not self.overrides:
            return
        lines = ["services:", "  api:", "    environment:"]
        for item in self.overrides:
            lines.append(f"      {item.key}: {json.dumps(item.value)}")
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        self.files.append(path)

    def refuse_images(self) -> Dict[str, Any]:
        """Each tagged image already here: its profile label must be this run's."""
        found = {}
        for service, repository in IMAGES.items():
            image = f"{repository}:{self.tag}"
            seen = self.docker.image_labels(image)
            found[service] = None if seen is None else seen["id"]
            if seen is None:
                continue
            label = seen["labels"].get(config.IMAGE_PROFILE_LABEL)
            if label != self.profile:
                raise guard.Refused(
                    f"{image} is labelled profile {label!r}, not {self.profile!r}:"
                    " an image tagged under this run's name is not reused"
                )
        return found

    # -- up and down ---------------------------------------------------------------

    def up(self) -> None:
        compose_text = (self.tree / COMPOSE_FILE).read_text(encoding="utf-8")
        refuse_unasked_files(self.tree)
        # The video's logs directory is made by the first child that logs;
        # the override file is written before any child has run.
        self.logs.mkdir(parents=True, exist_ok=True)
        self.write_override(self.logs / "compose.override.yml")
        refuse_compose_env(self.compose_env, compose_text)
        if self.project in self.docker.projects():
            raise guard.Refused(
                f"compose project {self.project} exists already; it is not reused"
            )
        before = self.refuse_images()
        self.facts["images_before"] = before
        self.record = record_project(self.children.work, self.project, self.files)
        began = time.monotonic()
        status = self.docker.cmd(
            "compose-up", self._compose("up", "-d", "--wait"), timeout=UP_TIMEOUT_S
        )
        self.facts["up_seconds"] = round(time.monotonic() - began, 1)
        self.facts["built"] = sorted(s for s, ident in before.items() if ident is None)
        if status != 0:
            raise StackError(
                f"docker compose up exited {status}; see {self.logs}/compose-up.log"
            )
        self.check_published()
        self.check_images()
        self.check_profile()
        wait_until(f"{self.dashboard_url}/", 60)
        for app in self.apps:
            self.start_app(app)

    def check_published(self) -> None:
        ps = json_lines(
            self.docker.out("compose-ps", self._compose("ps", "--format", "json"))
        )
        found = published(ps)
        self.facts["published"] = {s: [list(p) for p in v] for s, v in found.items()}
        problems = published_problems(found, self.ports)
        if problems:
            raise StackError("PE C3: " + "; ".join(problems))

    def check_images(self) -> None:
        """The containers run the tagged images, each labelled this profile."""
        ps = {
            str(entry.get("Service")): entry
            for entry in json_lines(
                self.docker.out("compose-ps", self._compose("ps", "--format", "json"))
            )
        }
        images: Dict[str, Any] = {"tag": self.tag, "commit": self.commit}
        for service, repository in IMAGES.items():
            image = f"{repository}:{self.tag}"
            seen = self.docker.image_labels(image)
            if seen is None:
                raise StackError(f"{image} is not here after up")
            label = seen["labels"].get(config.IMAGE_PROFILE_LABEL)
            if label != self.profile:
                raise StackError(f"{image} is labelled profile {label!r}")
            entry = ps.get(service) or {}
            running = ""
            if entry.get("ID"):
                running = self.docker.out(
                    f"inspect-{service}",
                    ["docker", "inspect", "--format", "{{.Image}}", str(entry["ID"])],
                ).strip()
            if entry.get("Image") != image or running != seen["id"]:
                raise StackError(
                    f"the {service} container runs {entry.get('Image')!r}"
                    f" ({running or 'no image'}), not {image} ({seen['id']})"
                )
            images[service] = {"image": image, "id": seen["id"]}
        self.facts["images"] = images

    def check_profile(self) -> None:
        served = json.loads(_get(f"{self.api_url}/api/v1/modules")).get("profile")
        self.facts["served_profile"] = served
        if served != self.profile:
            raise StackError(
                f"GET /api/v1/modules answers profile {served!r}, not {self.profile!r}"
            )

    def down(self) -> None:
        for app in self.apps:
            self.children.stop(f"app-{app}")
        left = remove_project(self.docker, self.project, self.files, self.children.work)
        self.facts["left"] = left
        if any(left.values()):
            raise StackError(f"{self.project}: left after down: {left}")
        if self.record is not None and self.record.exists():
            self.children.work.remove(self.record)

    def manifest(self) -> Dict[str, Any]:
        """The manifest's ``stack``: what the approval record names (EM C1, C5)."""
        return {
            "name": self.name,
            "project": self.project,
            "image_tag": self.tag,
            "images": self.facts.get("images"),
            "built": self.facts.get("built"),
            "up_seconds": self.facts.get("up_seconds"),
            "published": self.facts.get("published"),
            "overrides": sorted(o.key for o in self.overrides),
        }

    # -- the demo apps -----------------------------------------------------------

    def seed_app(self, app: str, *, history: bool) -> Optional[str]:
        """Off camera: the app's seed, in the API container. Its compose key is
        checked; for StreamPulse the server key is read into this process and
        returned (else None)."""
        module, _variable = config.APP_SEED[app]
        status = self.docker.cmd(
            f"seed-{app}",
            self._compose(
                "exec",
                "-T",
                "api",
                "python",
                "-m",
                module,
                *([] if history else ["--no-history"]),
            ),
            timeout=1200,
        )
        if status != 0:
            raise StackError(
                f"{module} exited {status}; see {self.logs}/seed-{app}.log"
            )
        registered = self.read_in_api(f"{KEY_DIR_IN_API}/{app}/.api_key")
        if registered != config.APP_STATIC_KEY[app]:
            raise StackError(
                f"seed {app}: the key it registered is not the compose key the"
                " app was built with"
            )
        if app != "streampulse":
            return None
        return self.read_in_api(f"{KEY_DIR_IN_API}/{app}/{config.SERVER_KEY_FILE}")

    def read_in_api(self, path: str) -> str:
        """A key file in the API container, stripped, into this process only (PE C7)."""
        text = self.docker.out(
            "read-key", self._compose("exec", "-T", "api", "cat", path), private=True
        )
        return key_text(text, Path(path).name)

    def start_app(self, app: str) -> None:
        tree = self.apps[app]
        argv = build.app_start_argv(tree, app, self.ports.app(app))
        build.refuse_app_argv(argv, tree, app, self.ports.app(app))
        self.children.start(
            f"app-{app}",
            argv,
            cwd=build.app_dir(tree, app),
            env=self.app_envs[app],
            log=self.logs / f"app-{app}.log",
            kind="app",
        )
        wait_until(f"{self.app_url(app)}/", 60)


def key_text(text: str, name: str) -> str:
    """``stack.read_key``'s rule on text already read: one key, stripped."""
    value = text.strip()
    if not value or any(ch.isspace() for ch in value):
        raise StackError(f"{name} does not hold one key")
    return value
