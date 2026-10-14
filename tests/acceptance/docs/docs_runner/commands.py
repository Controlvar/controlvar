"""A guide's shell block, run as its reader runs it (``run`` and ``finish`` steps).

Only on demo-local: that stack is a checkout ``demo/setup-local.sh`` has set
up, and the demo's documents tell their reader to run their commands "from
the repository root, with the venv active". So a ``run`` step's command is one
shell block of the section its ``doc`` names, exactly as the page writes it
(``block_problem``; the loader refuses any other text), and it runs:

* from the checkout's root (``Running.checkout``);
* under ``bash -e -c``, so a block of several lines runs as the page shows it;
* with the venv the script made active: its ``venv/bin`` first on ``PATH``
  and ``VIRTUAL_ENV`` set, as ``source venv/bin/activate`` would; a checkout
  without ``venv/bin`` fails the step;
* with nothing else of the runner's environment but ``HOME`` and the system
  ``PATH`` (``environment``), so a value the runner holds never reaches it;
* with stdin closed, stdout and stderr into one file outside the run
  directory, and in a process group of its own;
* for at most ``up_to_seconds`` from its start: past that its process group is
  stopped (SIGTERM, then SIGKILL after ``STOP_SECONDS``, sent only while its
  first process is unreaped, so the group id is still its own) and the step
  fails.

``background: true`` starts the block and goes on to the next step; the later
``finish`` step that names it waits for it to end, within the same bound, and
holds its exit status and output to that step's ``expect``. A block still
running when its journey ends (a step failed before its ``finish``) is
stopped the same way, so nothing a journey starts outlives it.

What the block printed is written to the run directory only through the run's
redactor (``execute.py``), cut to its last ``OUTPUT_CHARS`` characters.

Standard library only; nothing here imports Playwright, so the unit tests run
it for real against a scratch checkout.
"""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import IO, List, Mapping, Optional, Sequence

from docs_runner import sdk

#: The languages of a fenced block that is a shell block.
SHELL_LANGUAGES = ("bash", "sh", "shell")
#: How long a stopped block gets between SIGTERM and SIGKILL.
STOP_SECONDS = 10.0
#: How much of a block's output is kept, from its end, after redaction.
OUTPUT_CHARS = 20000


class CommandError(Exception):
    """The block cannot be run here; the message says why."""


def shell_blocks(section: str) -> List[str]:
    """The text of each shell block of a guide's section, in order, without the
    surrounding blank lines."""
    return [
        block.text.strip("\n")
        for block in sdk.blocks(section)
        if block.language in SHELL_LANGUAGES
    ]


def block_problem(section: str, command: str) -> Optional[str]:
    """Why *command* is not exactly one shell block of *section*, or None."""
    wanted = command.strip("\n")
    found = shell_blocks(section)
    if not found:
        return "the section has no shell block"
    if wanted not in found:
        return (
            "the command is not one of the section's shell blocks, word for word"
            f" ({len(found)} there)"
        )
    return None


def environment(checkout: Path, base: Mapping[str, str]) -> dict:
    """What the block sees: the checkout's venv active, and nothing else of *base*
    but ``HOME`` and its ``PATH``."""
    venv = checkout / "venv"
    path = base.get("PATH", "/usr/bin:/bin")
    return {
        "PATH": f"{venv / 'bin'}{os.pathsep}{path}",
        "VIRTUAL_ENV": str(venv),
        "HOME": base.get("HOME", str(checkout)),
        "LANG": "C.UTF-8",
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
    }


@dataclass
class Started:
    """A block that is running, or was."""

    command: str
    process: subprocess.Popen
    output: Path
    handle: IO[bytes]
    started: float
    up_to_seconds: int


@dataclass
class Finished:
    """How a block ended: its status (None when it was stopped), how long it
    ran, and what it printed."""

    status: Optional[int]
    seconds: float
    output: str
    stopped: bool = False

    def tail(self) -> str:
        text = self.output
        return text if len(text) <= OUTPUT_CHARS else "..." + text[-OUTPUT_CHARS:]


def start(
    command: str,
    checkout: Optional[Path],
    up_to_seconds: int,
    base: Mapping[str, str],
) -> Started:
    """Start *command* (a whole shell block) in *checkout*, or CommandError."""
    if checkout is None:
        raise CommandError(
            "the stack has no checkout to run a block in (only demo-local has one)"
        )
    if not (checkout / "venv" / "bin").is_dir():
        raise CommandError(
            "the checkout has no venv/bin to make active; demo/setup-local.sh makes it"
        )
    descriptor, name = tempfile.mkstemp(prefix="docs-journey-run-", suffix=".log")
    os.close(descriptor)
    output = Path(name)
    handle = output.open("wb")
    try:
        process = subprocess.Popen(
            ["bash", "-e", "-c", command],
            cwd=checkout,
            env=environment(checkout, base),
            stdin=subprocess.DEVNULL,
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    except OSError as error:
        handle.close()
        output.unlink(missing_ok=True)
        raise CommandError(
            f"bash could not be started ({type(error).__name__})"
        ) from None
    return Started(
        command=command,
        process=process,
        output=output,
        handle=handle,
        started=time.monotonic(),
        up_to_seconds=up_to_seconds,
    )


def _signal(process: subprocess.Popen, number: int) -> None:
    try:
        os.killpg(process.pid, number)
    except (ProcessLookupError, PermissionError):
        pass


def stop(started: Started) -> None:
    """Stop the block's process group if its first process is still running:
    SIGTERM to the group, then SIGKILL after ``STOP_SECONDS``.

    The group is signalled only while that process has not been reaped, so
    its id is still the group's and cannot name anyone else's processes.
    """
    process = started.process
    if process.poll() is not None:
        return
    _signal(process, signal.SIGTERM)
    try:
        process.wait(timeout=STOP_SECONDS)
    except subprocess.TimeoutExpired:
        _signal(process, signal.SIGKILL)
        process.wait()


def finish(started: Started) -> Finished:
    """Let the block end within its bound (stopping it past that); its outcome."""
    remaining = started.started + started.up_to_seconds - time.monotonic()
    stopped = False
    try:
        started.process.wait(timeout=max(remaining, 0))
    except subprocess.TimeoutExpired:
        stopped = True
    stop(started)
    return _ended(started, stopped)


def _ended(started: Started, stopped: bool) -> Finished:
    seconds = time.monotonic() - started.started
    started.handle.close()
    try:
        text = started.output.read_bytes().decode("utf-8", errors="replace")
    finally:
        started.output.unlink(missing_ok=True)
    return Finished(
        status=None if stopped else started.process.returncode,
        seconds=seconds,
        output=text,
        stopped=stopped,
    )


def abandon(started: Started) -> Finished:
    """Stop a block whose ``finish`` will not come (an earlier step failed);
    what it printed until then."""
    stopped = started.process.poll() is None
    stop(started)
    return _ended(started, stopped)


def run(
    command: str,
    checkout: Optional[Path],
    up_to_seconds: int,
    base: Mapping[str, str],
) -> Finished:
    """Run *command* to its end, within *up_to_seconds*."""
    return finish(start(command, checkout, up_to_seconds, base))


def judge(finished: Finished, exit_status: int, lines: Sequence[str]) -> List[str]:
    """What is wrong with how the block ended, against the step's expect."""
    problems: List[str] = []
    if finished.stopped:
        problems.append(
            f"it had not ended after {finished.seconds:.0f} s, its bound, so it was"
            " stopped"
        )
    elif finished.status != exit_status:
        problems.append(f"it exited {finished.status}, not {exit_status}")
    for line in lines:
        if line not in finished.output:
            problems.append(f"its output has no {line!r}")
    return problems
