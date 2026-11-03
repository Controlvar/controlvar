"""The Python SDK's driver: this checkout's ``sdk/python`` behind newline-delimited
JSON on stdin and stdout, spoken to by ``usecases/runner.py``.

One process holds one client. Each line in is one command; each line out is
its answer, one JSON object with ``ok`` true or false, in order:

* ``hello``: ``sdk``, ``version`` (the package's), ``module`` (the file the
  package was imported from, relative to the repository root) and
  ``capabilities``.
* ``init {api_url, api_key, mode, refresh_seconds?, cache_ttl_seconds?,
  timeout_ms?}``: the client, ``mode`` ``server`` or ``local``. In local mode
  it waits up to ``timeout_ms`` (5,000 unless given) for the first ruleset and
  answers ``ready``.
* ``evaluate {flag, user, context?, default}``: ``enabled``, ``value``,
  ``reason``, ``source``, ``error`` and ``ms``. ``source`` is ``local`` (from
  the ruleset), ``server`` (the SDK's server path; in server mode the SDK does
  not say whether its cache answered, so the proxy's count is the authority)
  or ``default`` (the call raised: ``enabled`` and ``value`` are the default
  and ``error`` names the exception's type and status). ``error`` is ``null``
  otherwise; it is never left out.
* ``status``: the SDK's ``status()``.
* ``close``: closes the client, answers, and exits 0. End of input does the
  same without an answer.

Refused but answered: ``unknown_command``, ``not_initialised`` (``evaluate`` or
``status`` before ``init``), ``already_initialised`` and ``bad_request`` (a
line that is not a JSON object, or arguments of the wrong type).

* **Identity.** ``sdk/python`` is put first on ``sys.path`` and the driver
  refuses to start (exit 3, before reading any command) if ``controlvar`` is
  imported from anywhere else, such as an installed copy.
* **The API key** arrives in ``init`` and is never written back: answers are
  built from named fields, and an error carries the exception's type and
  status, never its message.
* **stdout carries only answers.** ``sys.stdout`` is pointed at stderr before
  the SDK is imported, so nothing the SDK or Python prints can be read as an
  answer; the SDK's log goes to stderr.

Standard library and the SDK only (the SDK itself uses only the standard
library).
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[3]
SDK_ROOT = REPO_ROOT / "sdk" / "python"
#: The longest command line read.
MAX_LINE_BYTES = 64 * 1024
#: Local mode: how long ``init`` waits for the first ruleset unless told.
DEFAULT_READY_MS = 5000
#: Exit status when ``controlvar`` is not this checkout's.
NOT_THE_CHECKOUT = 3


class Refused(Exception):
    """A command answered ``ok: false`` with this code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def load_sdk() -> Any:
    """``controlvar`` from this checkout's ``sdk/python``; exits 3 when the
    import resolves anywhere else."""
    sys.path.insert(0, str(SDK_ROOT))
    import controlvar

    module = Path(controlvar.__file__).resolve()
    if not module.is_relative_to(SDK_ROOT / "controlvar"):
        sys.stderr.write(
            "controlvar was not imported from this checkout's sdk/python\n"
        )
        raise SystemExit(NOT_THE_CHECKOUT)
    return controlvar


def _error(exc: BaseException) -> dict[str, Any]:
    """An exception as an answer may carry it: its type and status only."""
    status = getattr(exc, "status", None)
    return {
        "type": type(exc).__name__,
        "status": status if isinstance(status, int) else None,
    }


def _number(args: dict[str, Any], name: str) -> float | None:
    value = args.get(name)
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise Refused("bad_request")
    return float(value)


def _string(args: dict[str, Any], name: str) -> str:
    value = args.get(name)
    if not isinstance(value, str) or not value:
        raise Refused("bad_request")
    return value


class Driver:
    def __init__(self, sdk: Any) -> None:
        self._sdk = sdk
        self._client: Any = None
        self.closed = False

    def handle(self, command: Any) -> dict[str, Any]:
        if not isinstance(command, dict) or not isinstance(command.get("cmd"), str):
            return {"ok": False, "error": "bad_request"}
        verb = command["cmd"]
        handler = getattr(self, f"_cmd_{verb}", None)
        if handler is None:
            return {"ok": False, "error": "unknown_command"}
        try:
            return {"ok": True, **handler(command)}
        except Refused as refused:
            return {"ok": False, "error": refused.code}

    def shutdown(self) -> None:
        if self._client is not None:
            self._client.close()
            self._client = None

    def _cmd_hello(self, args: dict[str, Any]) -> dict[str, Any]:
        module = Path(self._sdk.__file__).resolve().relative_to(REPO_ROOT)
        return {
            "sdk": "python",
            "version": self._sdk.__version__,
            "module": module.as_posix(),
            "capabilities": ["local-eval", "remote-eval"],
        }

    def _cmd_init(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            raise Refused("already_initialised")
        api_url, api_key = _string(args, "api_url"), _string(args, "api_key")
        mode = args.get("mode")
        if mode not in ("server", "local"):
            raise Refused("bad_request")
        options: dict[str, Any] = {"evaluation": mode}
        timeout_ms = _number(args, "timeout_ms")
        if timeout_ms is not None:
            options["timeout_seconds"] = timeout_ms / 1000
        cache_ttl = _number(args, "cache_ttl_seconds")
        if cache_ttl is not None:
            options["cache_ttl_seconds"] = cache_ttl
        refresh = _number(args, "refresh_seconds")
        if refresh is not None:
            options["refresh_interval_seconds"] = refresh
        try:
            client = self._sdk.ExperimentationClient(api_url, api_key, **options)
        except (TypeError, ValueError):
            raise Refused("bad_request") from None
        self._client = client
        answer: dict[str, Any] = {"mode": mode}
        if mode == "local":
            wait = (timeout_ms if timeout_ms is not None else DEFAULT_READY_MS) / 1000
            answer["ready"] = bool(client.ready(timeout_seconds=wait))
        return answer

    def _cmd_evaluate(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise Refused("not_initialised")
        flag, user = _string(args, "flag"), _string(args, "user")
        context = args.get("context")
        if context is not None and not isinstance(context, dict):
            raise Refused("bad_request")
        default = args.get("default", False)
        started = time.monotonic()
        try:
            evaluation = self._client.get_feature_flag(flag, user, context)
        except Exception as exc:  # reported in the answer, never swallowed
            return {
                "enabled": default,
                "value": default,
                "reason": None,
                "source": "default",
                "error": _error(exc),
                "ms": (time.monotonic() - started) * 1000,
            }
        return {
            "enabled": evaluation.enabled,
            "value": evaluation.value,
            "reason": evaluation.reason,
            "source": evaluation.source or "server",
            "error": None,
            "ms": (time.monotonic() - started) * 1000,
        }

    def _cmd_status(self, args: dict[str, Any]) -> dict[str, Any]:
        if self._client is None:
            raise Refused("not_initialised")
        status = self._client.status()
        refreshed = status.last_refresh_at
        return {
            "evaluation": status.evaluation,
            "ready": status.ready,
            "ruleset_version": status.ruleset_version,
            "last_refresh_at": refreshed.isoformat() if refreshed else None,
            "last_error": _error(status.last_error) if status.last_error else None,
            "server_evaluated_flags": list(status.server_evaluated_flags),
        }

    def _cmd_close(self, args: dict[str, Any]) -> dict[str, Any]:
        self.shutdown()
        self.closed = True
        return {}


def main() -> int:
    out = sys.stdout
    sys.stdout = sys.stderr
    driver = Driver(load_sdk())
    stdin = sys.stdin.buffer
    while not driver.closed:
        raw = stdin.readline(MAX_LINE_BYTES + 1)
        if not raw:
            break
        if len(raw) > MAX_LINE_BYTES:
            answer: dict[str, Any] = {"ok": False, "error": "bad_request"}
            # The rest of the over-long line is not a command.
            while raw and not raw.endswith(b"\n"):
                raw = stdin.readline(MAX_LINE_BYTES + 1)
        else:
            try:
                command = json.loads(raw)
            except ValueError:
                answer = {"ok": False, "error": "bad_request"}
            else:
                answer = driver.handle(command)
        out.write(json.dumps(answer, separators=(",", ":")) + "\n")
        out.flush()
    driver.shutdown()
    return 0


if __name__ == "__main__":
    sys.exit(main())
