"""Every program the tool starts goes through ``Children``.

Before it starts, its argv and environment are checked for a reserved port
(``guard.refuse_reserved_in``) and for a value the run keeps to itself
(``Children.private``: the StreamPulse server key), and what it was started
with is recorded: its kind, the argv and the names of its environment
variables (never their values), which the manifest carries and
``gates.child_environments`` checks against its kind's allow-list.

The long-running ones (the API, the static server) are written to
``<work root>/children.json`` while they run. A run that is killed leaves them
behind; the next run, holding the lock, stops each one whose command line
still names the work root, and no other process.

Python children start through ``LAUNCHER``: it removes setuptools' editable
finder (an editable install of the repository maps ``backend`` and
``modules`` to the developer's checkout, whatever the current directory),
asserts that ``backend`` is the copy's and, for the core profile, that no
``modules`` package can be found, says so on its first line of output, and
only then runs the module.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from showcase.capture import guard

#: The first line every Python child writes, then its module runs.
LAUNCHER_MARK = "showcase-launcher:"
LAUNCHER = r"""
import importlib.util, os, runpy, sys
root = os.path.realpath(sys.argv[1])
if os.path.realpath(os.getcwd()) != root:
    sys.exit(f"showcase-launcher: started in {os.getcwd()}, not in {root}")
def editable(entry):
    name = getattr(entry, "__qualname__", None) or getattr(entry, "__name__", None)
    return "editable" in str(name or type(entry).__name__).lower()
sys.meta_path[:] = [f for f in sys.meta_path if not editable(f)]
sys.path_hooks[:] = [h for h in sys.path_hooks if not editable(h)]
sys.path_importer_cache.clear()
sys.path[:] = [root] + [p for p in sys.path if p not in ("", root)]
import backend
where = os.path.realpath(backend.__file__)
if not where.startswith(root + os.sep):
    sys.exit(f"showcase-launcher: backend is {where}, not under {root}")
spec = importlib.util.find_spec("modules")
found = os.path.realpath(spec.origin) if spec and spec.origin else (None if spec is None else "namespace")
profile = os.environ.get("EXPERIMENTLY_PROFILE", "")
if profile == "core" and spec is not None:
    sys.exit(f"showcase-launcher: core copy, but modules imports from {found}")
if profile == "full" and not (found or "").startswith(root + os.sep):
    sys.exit(f"showcase-launcher: full copy, but modules is {found}")
print(f"showcase-launcher: backend={where} modules={found}", flush=True)
module, *args = sys.argv[2:]
sys.argv = [module, *args]
runpy.run_module(module, run_name="__main__", alter_sys=True)
"""


class ChildFailed(RuntimeError):
    pass


class Children:
    def __init__(self, work: guard.WorkRoot, parent: Mapping[str, str]):
        self.work = work
        self.parent = dict(parent)
        self.started: List[Dict[str, object]] = []
        self.running: Dict[str, subprocess.Popen] = {}
        #: The run-to-completion child in progress (a build, a seed), if any.
        self.current: Optional[subprocess.Popen] = None
        self.registry = work.path / "children.json"
        #: Values no child is given, on its argv or in its environment (PE 5).
        self.private: List[str] = []

    # -- recording -----------------------------------------------------------

    def _record(
        self, name: str, argv: Sequence[str], env: Mapping[str, str], kind: str
    ) -> None:
        """Refused before it starts: a reserved port, a private value, or a
        variable off its kind's allow-list."""
        from showcase.capture import gates

        guard.refuse_reserved_in(argv, env, what=name)
        for value in self.private:
            if any(value in str(arg) for arg in argv):
                raise guard.Refused(f"{name}: a private key is on its argv")
            if any(value in str(v) for v in env.values()):
                raise guard.Refused(f"{name}: a private key is in its environment")
        ok, _numbers, why = gates.child_environments(
            [{"name": name, "kind": kind, "env_keys": sorted(env)}]
        )
        if not ok:
            raise guard.Refused(f"EM condition 10: {why}")
        self.started.append(
            {
                "name": name,
                "kind": kind,
                "argv": [str(a) for a in argv],
                "env_keys": sorted(env),
            }
        )

    # -- starting ------------------------------------------------------------

    def run(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log: Path,
        kind: str,
        timeout: float = 900,
    ) -> int:
        """Run to completion, output to *log*; the exit status.

        In a process group of its own, which is stopped whole if the run ends
        first (``npm run build`` starts the Next.js build as a grandchild).
        """
        self._record(name, argv, env, kind)
        log.parent.mkdir(parents=True, exist_ok=True)
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(str(a) for a in argv)}\n")
            handle.flush()
            process = subprocess.Popen(
                [str(a) for a in argv],
                cwd=cwd,
                env=dict(env),
                stdout=handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
            self.current = process
            try:
                return process.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                handle.write(f"(stopped after {timeout:.0f} s)\n")
                return 124
            finally:
                self.current = None
                if process.poll() is None:
                    _stop_group(process)

    def capture(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log: Path,
        kind: str,
        timeout: float = 300,
        private: bool = False,
    ) -> Tuple[int, str]:
        """Run to completion; ``(exit status, standard output)``, the output
        held in this process. The log gets the command and its standard error,
        and, unless *private* (a key file read, PE C7 of T166), its output."""
        self._record(name, argv, env, kind)
        log.parent.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(
                [str(a) for a in argv],
                cwd=cwd,
                env=dict(env),
                capture_output=True,
                text=True,
                timeout=timeout,
                start_new_session=True,
            )
        except subprocess.TimeoutExpired:
            status, stdout, stderr = 124, "", f"(stopped after {timeout:.0f} s)\n"
        else:
            status, stdout, stderr = done.returncode, done.stdout, done.stderr
        with log.open("a", encoding="utf-8") as handle:
            handle.write(f"$ {' '.join(str(a) for a in argv)}\n")
            handle.write("(output held in memory, not logged)\n" if private else stdout)
            handle.write(stderr)
            handle.write(f"(exit {status})\n")
        return status, stdout

    def python(
        self, name: str, module: str, args: Sequence[str], *, cwd: Path, **kw
    ) -> int:
        return self.run(name, python_argv(cwd, module, args), cwd=cwd, **kw)

    def start(
        self,
        name: str,
        argv: Sequence[str],
        *,
        cwd: Path,
        env: Mapping[str, str],
        log: Path,
        kind: str,
    ) -> subprocess.Popen:
        """Start a long-running child, output to *log*."""
        self._record(name, argv, env, kind)
        log.parent.mkdir(parents=True, exist_ok=True)
        handle = log.open("a", encoding="utf-8")
        process = subprocess.Popen(
            [str(a) for a in argv],
            cwd=cwd,
            env=dict(env),
            stdout=handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        handle.close()
        self.running[name] = process
        self._save()
        return process

    def _save(self) -> None:
        entries = [
            {"name": name, "pid": process.pid}
            for name, process in self.running.items()
            if process.poll() is None
        ]
        self.registry.write_text(json.dumps(entries), encoding="utf-8")

    # -- stopping ------------------------------------------------------------

    def stop(self, name: str) -> None:
        process = self.running.pop(name, None)
        if process is None:
            return
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        self._save()

    def stop_all(self) -> None:
        current = self.current
        if current is not None and current.poll() is None:
            _stop_group(current)
        for name in list(self.running):
            self.stop(name)

    def stop_leftovers(self) -> List[int]:
        """Stop what a killed run left, if its command line names the work root."""
        stopped: List[int] = []
        if not self.registry.is_file():
            return stopped
        try:
            entries = json.loads(self.registry.read_text(encoding="utf-8"))
        except ValueError:
            entries = []
        for entry in entries:
            pid = int(entry.get("pid", 0))
            if pid <= 1:
                continue
            command = command_of(pid)
            if command is None or str(self.work.path) not in command:
                continue
            try:
                os.killpg(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                continue
            for _ in range(50):
                if command_of(pid) is None:
                    break
                time.sleep(0.1)
            else:
                try:
                    os.killpg(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            stopped.append(pid)
        self.registry.write_text("[]", encoding="utf-8")
        return stopped


def _stop_group(process: subprocess.Popen) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
        process.wait(timeout=15)
    except ProcessLookupError:
        return
    except subprocess.TimeoutExpired:
        os.killpg(process.pid, signal.SIGKILL)
        process.wait(timeout=5)


def python_argv(root: Path, module: str, args: Sequence[str]) -> List[str]:
    """A Python child: the launcher, the copy it must import from, the module."""
    return [sys.executable, "-c", LAUNCHER, str(root), module, *args]


def command_of(pid: int) -> Optional[str]:
    done = subprocess.run(
        ["ps", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        env=guard.tool_env(),
    )
    text = done.stdout.strip()
    return text or None
