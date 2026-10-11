"""One video's stack: a throwaway Postgres, the API and the dashboard, from the copy.

* Postgres: ``postgres:16-alpine`` with the tool's label, published on
  127.0.0.1:<postgres port> only, its data directory a tmpfs (no Docker volume,
  so nothing is left on disk), started with ``--rm`` and removed by name.
  ``docker port`` must answer exactly the tool's port (gate 15b) and the
  container may have no volume mount.
* The schema and the first administrator: ``python -m backend.app.db.bootstrap``;
  the demo data: ``backend.scripts.seed_demo_data`` (compose's ``SEED=demo``).
* The API: ``uvicorn backend.app.main:app`` on 127.0.0.1:<api port>.
* The dashboard: the copy's ``frontend/tests/e2e/static-server.mjs`` over the
  static export, every address given as a flag (its defaults are a developer's
  3100 and localhost:8000).

Every Python child starts through ``procs.LAUNCHER`` from the copy. After the
API answers, ``/api/v1/modules`` must report the profile asked for.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Mapping, Optional

from showcase.capture import config, guard
from showcase.capture.procs import Children


class StackError(RuntimeError):
    pass


@dataclass(frozen=True)
class Ports:
    api: int
    dashboard: int
    postgres: int

    def as_dict(self) -> Dict[str, int]:
        return {"API": self.api, "dashboard": self.dashboard, "Postgres": self.postgres}


def _get(url: str, timeout: float = 10) -> bytes:
    with guard.http_opener().open(url, timeout=timeout) as answer:
        return answer.read()


def wait_until(url: str, seconds: float, *, status_ok: bool = True) -> None:
    deadline = time.monotonic() + seconds
    last = ""
    while time.monotonic() < deadline:
        try:
            _get(url, timeout=5)
            return
        except urllib.error.HTTPError as error:
            if not status_ok:
                return
            last = f"HTTP {error.code}"
        except (urllib.error.URLError, OSError) as error:
            last = str(error)
        time.sleep(0.5)
    raise StackError(f"{url} did not answer within {seconds:.0f} s ({last})")


def labelled_containers(
    children: Children, env: Mapping[str, str], root: Path
) -> List[str]:
    """The containers this work root's runs started: the label's value is the root."""
    done = subprocess.run(
        [
            "docker",
            "ps",
            "-a",
            "--filter",
            f"label={config.CONTAINER_LABEL}={root}",
            "--format",
            "{{.Names}}",
        ],
        capture_output=True,
        text=True,
        env=dict(env),
        check=False,
    )
    if done.returncode != 0:
        raise StackError(f"docker ps failed: {done.stderr.strip()}")
    return [line for line in done.stdout.split() if line]


def remove_labelled(
    children: Children, env: Mapping[str, str], log: Path, root: Path
) -> List[str]:
    """Remove the containers with this work root's label (a killed run's), and no other.

    The run lock is per work root, so a run from another work root is never
    touched."""
    names = labelled_containers(children, env, root)
    for name in names:
        children.run(
            "docker-rm", ["docker", "rm", "-f", name], cwd=Path.cwd(), env=env, log=log
        )
    return names


class Stack:
    def __init__(
        self,
        *,
        tree: Path,
        ports: Ports,
        profile: str,
        run_id: str,
        children: Children,
        logs: Path,
        parent: Mapping[str, str],
    ):
        self.tree = tree
        self.ports = ports
        self.profile = profile
        self.container = f"showcase-pg-{run_id}"
        self.children = children
        self.logs = logs
        self.docker_env = guard.child_env({}, parent=parent, docker=True)
        self.api_env = guard.child_env(
            guard.api_settings(
                api_port=ports.api,
                dashboard_port=ports.dashboard,
                postgres_port=ports.postgres,
                profile=profile,
            ),
            parent=parent,
        )
        self.web_env = guard.child_env(
            guard.dashboard_settings(api_port=ports.api, profile=profile), parent=parent
        )
        self.facts: Dict[str, object] = {}

    @property
    def api_url(self) -> str:
        return f"http://127.0.0.1:{self.ports.api}"

    @property
    def dashboard_url(self) -> str:
        return f"http://127.0.0.1:{self.ports.dashboard}"

    # -- Postgres ------------------------------------------------------------

    def _docker(self, *args: str, log: str = "docker.log", timeout: float = 300) -> int:
        return self.children.run(
            "docker",
            ["docker", *args],
            cwd=self.tree,
            env=self.docker_env,
            log=self.logs / log,
            timeout=timeout,
        )

    def _docker_out(self, *args: str) -> str:
        guard.refuse_reserved_in(["docker", *args], self.docker_env, what="docker")
        done = subprocess.run(
            ["docker", *args],
            capture_output=True,
            text=True,
            env=self.docker_env,
            check=False,
        )
        if done.returncode != 0:
            raise StackError(f"docker {args[0]} failed: {done.stderr.strip()}")
        return done.stdout

    def start_postgres(self) -> None:
        status = self._docker(
            "run",
            "-d",
            "--rm",
            "--name",
            self.container,
            "--label",
            f"{config.CONTAINER_LABEL}={self.children.work.path}",
            "--tmpfs",
            "/var/lib/postgresql/data:rw",
            # Postgres listens on the tool's port inside the container too,
            # so no argument names 5432 at all (guard.refuse_reserved_in).
            "-p",
            f"127.0.0.1:{self.ports.postgres}:{self.ports.postgres}",
            "-e",
            "POSTGRES_USER=postgres",
            "-e",
            "POSTGRES_PASSWORD=postgres",
            "-e",
            "POSTGRES_DB=experimentation",
            "-e",
            f"PGPORT={self.ports.postgres}",
            config.POSTGRES_IMAGE,
            "-p",
            str(self.ports.postgres),
        )
        if status != 0:
            raise StackError(f"docker run {config.POSTGRES_IMAGE} exited {status}")
        mounts = json.loads(
            self._docker_out("inspect", "--format", "{{json .Mounts}}", self.container)
        )
        volumes = [m for m in mounts or [] if m.get("Type") == "volume"]
        if volumes:
            raise StackError(
                f"the Postgres container has a Docker volume ({volumes[0].get('Name')});"
                " its data must be a tmpfs"
            )
        published = self._docker_out("port", self.container).split()
        ports = sorted(
            {entry.rsplit(":", 1)[-1] for entry in published if ":" in entry}
        )
        self.facts["docker_port"] = ports
        if ports != [str(self.ports.postgres)]:
            raise StackError(
                f"docker port {self.container} publishes {ports}, not"
                f" [{self.ports.postgres}]"
            )
        deadline = time.monotonic() + 60
        ready = [
            "docker",
            "exec",
            self.container,
            "pg_isready",
            "-U",
            "postgres",
            "-d",
            "experimentation",
            "-h",
            "127.0.0.1",
            "-p",
            str(self.ports.postgres),
        ]
        guard.refuse_reserved_in(ready, self.docker_env, what="pg_isready")
        while time.monotonic() < deadline:
            done = subprocess.run(
                ready, capture_output=True, env=self.docker_env, check=False
            )
            if done.returncode == 0:
                return
            time.sleep(0.5)
        raise StackError("Postgres did not become ready within 60 s")

    def stop_postgres(self) -> None:
        self._docker("rm", "-f", self.container, timeout=120)

    # -- the API -------------------------------------------------------------

    def seed(self) -> None:
        for name, module, args in (
            ("bootstrap", "backend.app.db.bootstrap", []),
            (
                "seed-demo",
                "backend.scripts.seed_demo_data",
                ["--api-url", self.api_url],
            ),
        ):
            status = self.children.python(
                name,
                module,
                args,
                cwd=self.tree,
                env=self.api_env,
                log=self.logs / f"{name}.log",
                timeout=600,
            )
            if status != 0:
                raise StackError(
                    f"{module} exited {status}; see {self.logs / name}.log"
                )
            self.facts[f"{name}_launcher"] = launcher_line(self.logs / f"{name}.log")

    def start_api(self) -> None:
        from showcase.capture.procs import python_argv

        self.children.start(
            "api",
            python_argv(
                self.tree,
                "uvicorn",
                [
                    "backend.app.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.ports.api),
                    "--no-access-log",
                ],
            ),
            cwd=self.tree,
            env=self.api_env,
            log=self.logs / "api.log",
        )
        wait_until(f"{self.api_url}/health", 180)
        self.facts["api_launcher"] = launcher_line(self.logs / "api.log")
        served = json.loads(_get(f"{self.api_url}/api/v1/modules")).get("profile")
        self.facts["served_profile"] = served
        if served != self.profile:
            raise StackError(
                f"GET /api/v1/modules answers profile {served!r}, not {self.profile!r}:"
                " the API is not running the copy"
            )

    def start_dashboard(self) -> None:
        frontend = self.tree / "frontend"
        self.children.start(
            "dashboard",
            [
                "node",
                frontend / "tests" / "e2e" / "static-server.mjs",
                "--dir",
                frontend / "out",
                "--manifest",
                frontend / ".next" / "routes-manifest.json",
                "--port",
                str(self.ports.dashboard),
                "--host",
                "127.0.0.1",
                "--api",
                self.api_url,
            ],
            cwd=frontend,
            env=self.web_env,
            log=self.logs / "dashboard.log",
        )
        wait_until(f"{self.dashboard_url}/", 60)

    # -- the whole thing -----------------------------------------------------

    def up(self) -> None:
        self.start_postgres()
        self.seed()
        self.start_api()
        self.start_dashboard()

    def down(self) -> None:
        self.children.stop("dashboard")
        self.children.stop("api")
        self.stop_postgres()


def launcher_line(log: Path) -> Optional[str]:
    """The launcher's own line in a child's log: where ``backend`` and ``modules`` came from."""
    if not log.is_file():
        return None
    for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("showcase-launcher:"):
            return line
    return None
