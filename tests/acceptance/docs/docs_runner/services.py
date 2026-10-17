"""Compose services watched as their reader leaves them running (``services`` steps).

Only on compose-demo: the stack started with the demo command that
``docs/getting-started/docker-compose-file.md`` gives hands the runner a
``ComposeServices`` for its compose project (``Running.compose``). A
``services`` step names services of ``docker-compose.yml`` (the demo's traffic
simulators) and a number of seconds, and ``watch`` holds them to what a reader
of the page expects of a stack left running:

* at the start, each service has exactly one container, and it is running
  (``docker inspect``'s ``State.Status`` is ``running``; ``restarting``,
  ``exited`` and ``paused`` are not);
* every ``POLL_SECONDS`` until ``for_seconds`` have passed, each is still
  running and its ``RestartCount`` has not moved: a simulator that exits at
  start (``exit 1`` with "the shoplab seed has not run") is restarted by its
  ``restart: unless-stopped`` policy and fails here, as does one stopped in the
  window. Only the count's change is held, never its value: a stack recovered
  without ``down -v`` keeps the restarts it had before;
* with ``growing: events``, the rows of the stack's events table
  (``COUNTS``, read with ``psql`` in the ``postgres`` service) are more at the
  end than at the start: the simulators' traffic reaches the database.

The first problem ends the watch. The step's file holds each service's state
at the start and at the end, the counts, the problems, and, for a service that
failed, the last ``LOG_LINES`` lines it printed (through the run's redactor,
like every file the runner writes). The time is the property held, not a wait
for something to happen: it is bounded (``model.Services``) and the step's log
line says it.

Standard library only; the unit tests give it a fake ``docker`` on ``PATH``.
"""

from __future__ import annotations

import json
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

#: How often the services are inspected within the step's time, in seconds.
POLL_SECONDS = 5.0
#: How many of a failed service's last log lines the step's file keeps.
LOG_LINES = 20
#: How long one docker command may take.
DOCKER_SECONDS = 60.0
#: What ``growing`` may count: the SQL that counts it, run with ``psql`` in the
#: ``postgres`` service (the compose file's database, schema ``experimentation``).
COUNTS: Dict[str, str] = {
    "events": "SELECT count(*) FROM experimentation.events",
}
#: ``psql`` in the ``postgres`` service, as the user and database its own
#: environment names (the compose file sets both).
PSQL = 'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -tAc "$0"'
#: Prints "absent" when the file named as ``$0`` does not exist (``file_state``).
FILE_TEST = 'test -e "$0" || echo absent'


class ServicesError(Exception):
    """A docker command did not answer as expected; the message says which."""


@dataclass(frozen=True)
class State:
    """One service's containers, as ``docker compose ps`` and ``docker inspect`` give them."""

    service: str
    #: How many containers the service has (stopped ones included).
    containers: int
    #: The one container's ``State.Status`` ("" when it has none or several).
    status: str
    #: The one container's ``RestartCount`` (-1 when it has none or several).
    restarts: int

    def record(self) -> Dict[str, Any]:
        return {
            "containers": self.containers,
            "status": self.status,
            "restarts": self.restarts,
        }


class ComposeServices:
    """The docker commands a ``services`` step needs, for one compose project.

    ``compose`` is the start of every ``docker compose`` command (``docker
    compose -p <project> --profile demo``), run in ``cwd`` with ``env`` (the
    stack's own: nothing else of the caller's). Each command is appended to
    ``log``.
    """

    def __init__(
        self,
        compose: Sequence[str],
        cwd: Path,
        env: Mapping[str, str],
        log: Path,
    ):
        self.compose = list(compose)
        self.cwd = cwd
        self.env = dict(env)
        self.log = log

    def _run(self, argv: Sequence[str]) -> Tuple[int, str]:
        self.log.parent.mkdir(parents=True, exist_ok=True)
        with self.log.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(argv)}\n")
        try:
            done = subprocess.run(
                list(argv),
                cwd=self.cwd,
                env=self.env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=DOCKER_SECONDS,
                check=False,
            )
        except subprocess.TimeoutExpired:
            raise ServicesError(
                f"{' '.join(argv[:4])} did not answer within {DOCKER_SECONDS:.0f} s"
            ) from None
        except OSError as error:
            raise ServicesError(
                f"{argv[0]} could not be started ({type(error).__name__})"
            ) from None
        out = done.stdout.decode("utf-8", errors="replace")
        if done.returncode != 0:
            err = done.stderr.decode("utf-8", errors="replace").strip()
            with self.log.open("a", encoding="utf-8") as handle:
                handle.write(f"(exited {done.returncode}: {err[-500:]})\n")
        return done.returncode, out

    def state(self, service: str) -> State:
        """The service's containers and, when it has one, its status and restarts."""
        status, out = self._run([*self.compose, "ps", "-a", "-q", service])
        if status != 0:
            raise ServicesError(f"docker compose ps {service} exited {status}")
        ids = [line.strip() for line in out.splitlines() if line.strip()]
        if len(ids) != 1:
            return State(service, len(ids), "", -1)
        status, out = self._run(
            [
                "docker",
                "inspect",
                "--format",
                "{{json .State.Status}} {{.RestartCount}}",
                ids[0],
            ]
        )
        if status != 0:
            raise ServicesError(
                f"docker inspect of {service}'s container exited {status}"
            )
        try:
            raw_status, raw_restarts = out.strip().rsplit(" ", 1)
            return State(service, 1, str(json.loads(raw_status)), int(raw_restarts))
        except ValueError:
            raise ServicesError(
                f"docker inspect of {service}'s container answered {out.strip()[:80]!r}"
            ) from None

    def count(self, what: str) -> int:
        """The number ``COUNTS[what]`` counts."""
        status, out = self._run(
            [*self.compose, "exec", "-T", "postgres", "sh", "-c", PSQL, COUNTS[what]]
        )
        text = out.strip()
        if status != 0 or not text.isdigit():
            raise ServicesError(
                f"counting the {what} exited {status} and printed {text[:80]!r}"
            )
        return int(text)

    def file_state(self, service: str, path: str) -> str:
        """Whether *path* exists in *service*'s container: "absent", "present",
        or "unreadable (...)" when docker could not say (no container, say).

        "absent" is printed by the container's own shell only when the test
        ran and failed, so a command that never reached the container cannot
        read as absent.
        """
        status, out = self._run(
            [*self.compose, "exec", "-T", service, "sh", "-c", FILE_TEST, path]
        )
        if status != 0:
            return f"unreadable (exit {status})"
        return "absent" if out.strip() == "absent" else "present"

    def logs(self, service: str, lines: int = LOG_LINES) -> str:
        """The service's last *lines* lines of output ("" when docker answers nothing)."""
        _, out = self._run(
            [
                *self.compose,
                "logs",
                "--no-color",
                "--no-log-prefix",
                "--tail",
                str(lines),
                service,
            ]
        )
        return "\n".join(out.splitlines()[-lines:])


@dataclass
class Watched:
    """What a ``services`` step saw: the states, the counts, the problems."""

    services: List[str]
    for_seconds: int
    growing: Optional[str]
    start: Dict[str, State] = field(default_factory=dict)
    end: Dict[str, State] = field(default_factory=dict)
    counts: List[int] = field(default_factory=list)
    problems: List[str] = field(default_factory=list)
    logs: Dict[str, str] = field(default_factory=dict)
    seconds: float = 0.0

    def record(self) -> Dict[str, Any]:
        return {
            "services": {
                name: {
                    "start": self.start[name].record() if name in self.start else None,
                    "end": self.end[name].record() if name in self.end else None,
                }
                for name in self.services
            },
            "for_seconds": self.for_seconds,
            "watched_seconds": round(self.seconds, 1),
            "growing": self.growing,
            "counts": self.counts,
            "problems": self.problems,
            "logs": self.logs,
        }

    def summary(self) -> str:
        restarts = ", ".join(
            f"{name} {self.end[name].restarts}"
            for name in self.services
            if name in self.end
        )
        line = (
            f"{', '.join(self.services)} running for {self.seconds:.0f} s, restarts"
            f" unchanged ({restarts})"
        )
        if self.growing is not None and len(self.counts) == 2:
            line += f"; {self.growing} {self.counts[0]} -> {self.counts[1]}"
        return line


def _not_running(state: State) -> str:
    if state.containers != 1:
        return f"{state.service} has {state.containers} containers, not one"
    if state.status != "running":
        return f"{state.service} is {state.status}, not running"
    return ""


def watch(
    compose: ComposeServices,
    services: Sequence[str],
    for_seconds: float,
    growing: Optional[str] = None,
    *,
    every: float = POLL_SECONDS,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Watched:
    """Hold *services* running and unrestarted for *for_seconds*; see the module."""
    watched = Watched(list(services), int(for_seconds), growing)
    started = clock()
    failed: List[str] = []
    try:
        for name in services:
            watched.start[name] = watched.end[name] = compose.state(name)
            problem = _not_running(watched.start[name])
            if problem:
                watched.problems.append(problem)
                failed.append(name)
        if not watched.problems and growing is not None:
            watched.counts.append(compose.count(growing))
        deadline = started + for_seconds
        while not watched.problems and clock() < deadline:
            sleep(max(0.0, min(every, deadline - clock())))
            for name in services:
                now = compose.state(name)
                watched.end[name] = now
                problem = _not_running(now)
                if not problem and now.restarts != watched.start[name].restarts:
                    problem = (
                        f"{name} restarted: RestartCount"
                        f" {watched.start[name].restarts} -> {now.restarts}"
                        f" after {clock() - started:.0f} s"
                    )
                elif problem:
                    problem += f" after {clock() - started:.0f} s"
                if problem:
                    watched.problems.append(problem)
                    failed.append(name)
        if not watched.problems and growing is not None:
            watched.counts.append(compose.count(growing))
            if watched.counts[1] <= watched.counts[0]:
                watched.problems.append(
                    f"the {growing} did not grow in {for_seconds:.0f} s:"
                    f" {watched.counts[0]} -> {watched.counts[1]}"
                )
    except ServicesError as error:
        watched.problems.append(str(error))
    watched.seconds = clock() - started
    for name in failed:
        try:
            text = compose.logs(name)
        except ServicesError as error:
            text = f"(no logs: {error})"
        watched.logs[name] = text
        last = next((line for line in reversed(text.splitlines()) if line.strip()), "")
        if last:
            watched.problems.append(f"{name} last printed: {last.strip()[:200]}")
    return watched
