"""One SDK driver process, spoken to in newline-delimited JSON.

The protocol is PLAN-v2's SPEC S3 cut to five commands: ``hello``, ``init``,
``evaluate``, ``status`` and ``close``. A command is one JSON object on one
line of the driver's stdin, ``{"cmd": <verb>, ...}``; the answer is one JSON
object on one line of its stdout, with ``ok`` true or false, in order. The API
key arrives in ``init``, on stdin, never in the driver's arguments or
environment.

What the runner guarantees:

* **Every wait has a bound.** Each command's answer is awaited for
  ``timeout`` seconds (10 unless given); the driver's exit, the reaping of a
  killed driver and the reader threads' ends are awaited for bounded times
  too. Nothing here can hang the test session.
* **A hang, an exit before the answer, or an answer that is not one JSON
  object fails loudly**: the driver is killed and reaped, and ``DriverError``
  is raised. Its message names the command's verb and what went wrong, never
  the command or the answer.
* **stderr is the driver's log, and is never shown.** It is drained on a
  thread from the moment the driver starts, so a driver that logs a lot cannot
  block on a full pipe; only its size is kept (``stderr_bytes``).
* **What a test sees is redacted.** Every line the driver writes is redacted
  against ``secrets`` (the ``api_key`` of every ``init`` is added to them)
  before it is parsed, so a secret the driver writes back as it is never
  reaches an answer or an assertion message.
* **A write never blocks.** A command is at most ``MAX_COMMAND_BYTES``, below
  any pipe's buffer, and at most one command is ever unanswered.
"""

from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
from collections.abc import Iterable, Mapping, Sequence
from typing import IO, Any, NoReturn

#: Seconds each command's answer is awaited, unless the runner is given another.
DEFAULT_TIMEOUT = 10.0
#: The longest command line written, and the longest answer line read.
MAX_COMMAND_BYTES = 16 * 1024
MAX_ANSWER_BYTES = 64 * 1024
#: Seconds to wait for a killed driver to be reaped, and for a thread to end.
REAP_SECONDS = 5.0
#: The commands of the protocol.
VERBS = ("hello", "init", "evaluate", "status", "close")
REDACTED = "[redacted]"

#: What the stdout reader queues for an answer longer than MAX_ANSWER_BYTES.
_TOO_LONG = object()


class DriverError(Exception):
    """The driver broke the protocol. The message names the command's verb
    and what went wrong; never a command, an answer or a log line."""


def redact(text: str, secrets: Iterable[str]) -> str:
    """*text* with every occurrence of every non-empty secret replaced."""
    for secret in secrets:
        if secret:
            text = text.replace(secret, REDACTED)
    return text


class Runner:
    """One driver process: ``start`` it, ``call`` its commands, ``stop`` it
    (or use it as a context manager)."""

    def __init__(
        self,
        argv: Sequence[str],
        *,
        env: Mapping[str, str] | None = None,
        timeout: float = DEFAULT_TIMEOUT,
        secrets: Iterable[str] = (),
    ) -> None:
        self._argv = list(argv)
        # An explicit environment: PATH and nothing else unless given.
        self._env = (
            dict(env) if env is not None else {"PATH": os.environ.get("PATH", "")}
        )
        self.timeout = timeout
        self._secrets = [s for s in secrets if s]
        self._process: subprocess.Popen[bytes] | None = None
        self._lines: queue.Queue[Any] = queue.Queue()
        self._threads: list[threading.Thread] = []
        #: Bytes the driver wrote to stderr (never kept, never shown).
        self.stderr_bytes = 0

    # -- lifecycle ----------------------------------------------------------
    def start(self) -> Runner:
        if self._process is not None:
            raise RuntimeError("the driver is already started")
        self._process = subprocess.Popen(
            self._argv,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=self._env,
            close_fds=True,
        )
        for target, stream in (
            (self._read_stdout, self._process.stdout),
            (self._drain_stderr, self._process.stderr),
        ):
            thread = threading.Thread(target=target, args=(stream,), daemon=True)
            thread.start()
            self._threads.append(thread)
        return self

    @property
    def returncode(self) -> int | None:
        """The driver's exit status once it has been reaped, else ``None``."""
        return None if self._process is None else self._process.returncode

    def stop(self) -> None:
        """End the driver: close its stdin (end of input), wait up to
        ``timeout`` for it to exit, then kill it; always reap it."""
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=self.timeout)
            except subprocess.TimeoutExpired:
                pass
        self._kill_and_reap()

    def __enter__(self) -> Runner:
        return self.start()

    def __exit__(self, *exc_info: Any) -> None:
        self.stop()

    # -- the protocol ------------------------------------------------------------
    def call(self, verb: str, **args: Any) -> dict[str, Any]:
        """Send one command and return its answer (redacted), ``ok`` true or
        false. ``DriverError`` when the driver hangs, exits or answers
        something that is not one JSON object with a boolean ``ok``."""
        if verb not in VERBS:
            raise ValueError(f"not a command of the protocol: {verb!r}")
        process = self._process
        if process is None or process.stdin is None:
            raise RuntimeError("start the driver first")
        if verb == "init" and isinstance(args.get("api_key"), str) and args["api_key"]:
            self._secrets.append(args["api_key"])
        line = (
            json.dumps({"cmd": verb, **args}, separators=(",", ":")) + "\n"
        ).encode()
        if len(line) > MAX_COMMAND_BYTES:
            raise ValueError(
                f"the {verb} command is longer than {MAX_COMMAND_BYTES} bytes"
            )
        try:
            process.stdin.write(line)
            process.stdin.flush()
        except OSError:
            self._fail(f"the driver exited before it was sent {verb}")
        try:
            raw = self._lines.get(timeout=self.timeout)
        except queue.Empty:
            self._fail(
                f"the driver did not answer {verb} within {self.timeout:g} s (hung)"
            )
        if raw is None:
            self._fail(f"the driver exited before answering {verb}")
        if raw is _TOO_LONG:
            self._fail(
                f"the driver's answer to {verb} is longer than {MAX_ANSWER_BYTES} bytes"
            )
        text = redact(raw.decode("utf-8", errors="replace"), self._secrets)
        try:
            answer = json.loads(text)
        except ValueError:
            self._fail(f"the driver answered {verb} with a line that is not JSON")
        if not isinstance(answer, dict) or not isinstance(answer.get("ok"), bool):
            self._fail(f"the driver answered {verb} with no boolean 'ok'")
        return answer

    # -- internals ---------------------------------------------------------------
    def _fail(self, message: str) -> NoReturn:
        """Kill and reap the driver, then raise. The exit status is named."""
        self._kill_and_reap()
        code = self.returncode
        raise DriverError(f"{message} (exit status {code})") from None

    def _kill_and_reap(self) -> None:
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.kill()
        try:
            process.wait(timeout=REAP_SECONDS)
        except subprocess.TimeoutExpired:
            pass
        for thread in self._threads:
            thread.join(timeout=REAP_SECONDS)
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass

    def _read_stdout(self, stream: IO[bytes]) -> None:
        try:
            while True:
                raw = stream.readline(MAX_ANSWER_BYTES + 1)
                if not raw:
                    break
                if len(raw) > MAX_ANSWER_BYTES:
                    self._lines.put(_TOO_LONG)
                    break
                self._lines.put(raw)
        except (OSError, ValueError):
            pass
        finally:
            self._lines.put(None)

    def _drain_stderr(self, stream: IO[bytes]) -> None:
        try:
            while True:
                chunk = stream.read1(65536)  # type: ignore[attr-defined]
                if not chunk:
                    break
                self.stderr_bytes += len(chunk)
        except (OSError, ValueError):
            pass
