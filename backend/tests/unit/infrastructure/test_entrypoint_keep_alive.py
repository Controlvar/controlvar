"""The API keeps an idle connection open longer than the load balancer (#2107).

uvicorn closes an idle HTTP/1.1 connection 5 s after a response unless it is
started with ``--timeout-keep-alive``. The AWS load balancer in front of the
API keeps an idle connection open 60 s, so with uvicorn's default the API is
the side that closes, and the load balancer may already be sending the next
request on that connection. AWS lists a target keep-alive shorter than the
load balancer's idle timeout as a cause of 502s.

``backend/docker-entrypoint.sh`` therefore passes ``--timeout-keep-alive
${UVICORN_TIMEOUT_KEEP_ALIVE:-65}`` to uvicorn unless the command already has
one, and the preflight refuses a value that is not a whole number above 60.

The entrypoint runs here with ``python`` and ``uvicorn`` replaced by stubs
that record what they were given, started with the image's own command, read
out of ``backend/Dockerfile`` rather than retyped. One test then starts the
pinned uvicorn itself, on 127.0.0.1, with the arguments the entrypoint built,
and holds an idle connection open: not what the flag is called, what it does.
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Tuple

import pytest

from backend.app.core import preflight

REPO_ROOT = Path(__file__).resolve().parents[4]
ENTRYPOINT = REPO_ROOT / "backend" / "docker-entrypoint.sh"
DOCKERFILE = REPO_ROOT / "backend" / "Dockerfile"
BASH = shutil.which("bash")

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(
        BASH is None or not ENTRYPOINT.is_file() or not DOCKERFILE.is_file(),
        reason="needs bash, backend/docker-entrypoint.sh and backend/Dockerfile",
    ),
]

FLAG = "--timeout-keep-alive"

#: What the trusted-proxy helper prints here: the list uvicorn is started with.
TRUSTED = "127.0.0.1,::1"

#: ``python``: the trusted-proxy helper prints TRUSTED; the preflight is the
#: real one when REAL_PREFLIGHT is set and succeeds otherwise; anything else
#: succeeds. ``uvicorn``: writes each of its arguments on a line of $ARGS_LOG.
_PYTHON = """#!/usr/bin/env bash
case "$*" in
    "-m backend.app.core.proxy_trust")
        printf '%s\\n' "$TRUSTED"
        exit 0
        ;;
    "-m backend.app.core.preflight")
        if [ -n "${REAL_PREFLIGHT:-}" ]; then
            PYTHONPATH="$STUB_REPO_ROOT" exec "$STUB_REAL_PYTHON" -m backend.app.core.preflight
        fi
        exit 0
        ;;
esac
exit 0
"""
_UVICORN = """#!/usr/bin/env bash
printf '%s\\n' "$@" > "$ARGS_LOG"
"""


def image_command() -> List[str]:
    """The image's CMD, as backend/Dockerfile declares it (exec form)."""
    lines = [
        line
        for line in DOCKERFILE.read_text(encoding="utf-8").splitlines()
        if line.startswith("CMD ")
    ]
    assert len(lines) == 1, f"expected one CMD in backend/Dockerfile: {lines}"
    command = json.loads(lines[0][len("CMD ") :])
    assert command and command[0] == "uvicorn", command
    return command


def start(
    tmp_path: Path, *command: str, **env_vars: str
) -> Tuple[int, str, Optional[List[str]]]:
    """The real entrypoint with *command*: (status, stderr, uvicorn's
    arguments, or None when uvicorn was not started).

    The environment is built from nothing, so no ``UVICORN_*`` of the test
    run's reaches it; ``cd /app`` goes to *tmp_path*.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(parents=True)
    for name, body in (("python", _PYTHON), ("uvicorn", _UVICORN)):
        (bin_dir / name).write_text(body)
        (bin_dir / name).chmod(0o755)
    args_log = tmp_path / "uvicorn-args"
    env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "HOME": str(tmp_path),
        "ARGS_LOG": str(args_log),
        "APP_DIR_FOR_TEST": str(tmp_path),
        "WAIT_FOR_DB": "false",
        "RUN_MIGRATIONS": "false",
        "OPENSSL_armcap": "0",
        "ENVIRONMENT": "development",
        "TRUSTED": TRUSTED,
        "STUB_REAL_PYTHON": sys.executable,
        "STUB_REPO_ROOT": str(REPO_ROOT),
        **env_vars,
    }
    driver = (
        'cd() { builtin cd "$APP_DIR_FOR_TEST"; }; export -f cd; exec bash "$0" "$@"'
    )
    result = subprocess.run(
        [BASH, "-c", driver, str(ENTRYPOINT), *command],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        stdin=subprocess.DEVNULL,
    )
    started = args_log.read_text().splitlines() if args_log.exists() else None
    return result.returncode, result.stderr, started


def keep_alive_values(arguments: List[str]) -> List[str]:
    """Every keep-alive value in *arguments*, in either form, in order."""
    values = []
    for at, argument in enumerate(arguments):
        if argument == FLAG:
            values.append(arguments[at + 1] if at + 1 < len(arguments) else "")
        elif argument.startswith(FLAG + "="):
            values.append(argument[len(FLAG) + 1 :])
    return values


def start_line(stderr: str) -> str:
    lines = [
        line
        for line in stderr.splitlines()
        if line.startswith("[entrypoint] starting:")
    ]
    assert len(lines) == 1, stderr
    return lines[0]


# ---------------------------------------------------------------------------
# What the entrypoint hands uvicorn
# ---------------------------------------------------------------------------


@pytest.mark.regression
def test_the_image_command_starts_uvicorn_with_a_65_second_keep_alive_once(tmp_path):
    """V1: the Dockerfile's CMD passes arguments, so the default the
    entrypoint fills in when it is given none never runs in the image; the
    flag has to be added on the uvicorn branch."""
    code, stderr, started = start(tmp_path, *image_command())
    assert code == 0, stderr
    assert started is not None, stderr
    assert keep_alive_values(started) == ["65"], started
    # The line Docker Smoke and a deployed task's log are read for.
    assert f" {FLAG} 65 " in start_line(stderr) + " ", stderr
    # After --workers, before the proxy flags.
    assert started.index("--workers") < started.index(FLAG), started
    assert started.index(FLAG) < started.index("--proxy-headers"), started


@pytest.mark.parametrize(
    "callers",
    [[FLAG, "90"], [f"{FLAG}=90"]],
    ids=["separate", "equals"],
)
def test_a_keep_alive_the_command_passes_is_kept_and_not_added_to(tmp_path, callers):
    """V3: uvicorn keeps the last of two flags, so an added one would win
    without a word; the caller's is the only one."""
    code, stderr, started = start(tmp_path, "uvicorn", "backend.app.main:app", *callers)
    assert code == 0, stderr
    assert keep_alive_values(started) == ["90"], started


@pytest.mark.parametrize(
    "value, passed",
    [("120", "120"), ("", "65")],
    ids=["set", "empty"],
)
def test_the_variable_reaches_the_flag(tmp_path, value, passed):
    """V4: UVICORN_TIMEOUT_KEEP_ALIVE is what is passed; empty means unset."""
    code, stderr, started = start(
        tmp_path, *image_command(), UVICORN_TIMEOUT_KEEP_ALIVE=value
    )
    assert code == 0, stderr
    assert keep_alive_values(started) == [passed], started


def test_the_default_is_one_the_preflight_accepts(tmp_path):
    """V6: the value the entrypoint passes when nothing is set is the one the
    preflight's refusal tells an operator they get, and it accepts it."""
    code, stderr, started = start(tmp_path, *image_command())
    assert code == 0, stderr
    (passed,) = keep_alive_values(started)
    assert passed == str(preflight.KEEP_ALIVE_DEFAULT), started
    assert preflight.keep_alive_problem(passed) is None
    assert (
        preflight.check(
            {"ENVIRONMENT": "development", preflight.KEEP_ALIVE_SETTING: passed},
            full_profile=False,
            root=tmp_path,
        )
        == []
    )


@pytest.mark.parametrize(
    "command",
    [["python", "-m", "backend.app.db.bootstrap"], ["true"]],
    ids=["bootstrap", "true"],
)
def test_a_command_other_than_uvicorn_gets_no_keep_alive(tmp_path, command):
    """V7: the migration task and the chart's init container run other
    commands; the flag is uvicorn's."""
    code, stderr, started = start(tmp_path, *command)
    assert code == 0, stderr
    assert started is None, started
    assert start_line(stderr) == "[entrypoint] starting: " + " ".join(command)


@pytest.mark.regression
def test_a_refused_value_stops_the_start_even_when_the_command_passes_a_flag(
    tmp_path,
):
    """V5, through the entrypoint: the real preflight runs first and cannot
    see the command, so a refused UVICORN_TIMEOUT_KEEP_ALIVE stops the
    container (78) before uvicorn starts, whatever flag the command has."""
    code, stderr, started = start(
        tmp_path,
        "uvicorn",
        "backend.app.main:app",
        FLAG,
        "90",
        UVICORN_TIMEOUT_KEEP_ALIVE="30",
        REAL_PREFLIGHT="1",
    )
    assert (code, started) == (78, None), stderr
    assert "  - UVICORN_TIMEOUT_KEEP_ALIVE='30' must be a whole number" in stderr


# ---------------------------------------------------------------------------
# What the pinned uvicorn does with it
# ---------------------------------------------------------------------------

#: The module of the stub app the server is started with in place of the API.
STUB_MODULE = "keep_alive_probe_app"


def _stub_app(marker: bytes) -> str:
    """The stub app's source: it answers every request 200 with *marker*, and
    completes the lifespan."""
    return f"""
async def app(scope, receive, send):
    if scope["type"] == "lifespan":
        while True:
            message = await receive()
            if message["type"] == "lifespan.startup":
                await send({{"type": "lifespan.startup.complete"}})
            elif message["type"] == "lifespan.shutdown":
                await send({{"type": "lifespan.shutdown.complete"}})
                return
    elif scope["type"] == "http":
        await send({{
            "type": "http.response.start",
            "status": 200,
            "headers": [
                (b"content-type", b"text/plain"),
                (b"content-length", b"{len(marker)}"),
            ],
        }})
        await send({{"type": "http.response.body", "body": {marker!r}}})
"""


#: The ports this repository's own services use on a developer's machine
#: (the API, the dashboard and the demo apps, PostgreSQL, Redis).
AVOID_PORTS = frozenset({3000, 3100, 3200, 3300, 5432, 5433, 6379, 8000})

#: How long the test waits, idle, for the server to close the connection.
#: uvicorn's default closes it at 5 s; 65 closes it 57 s after this.
CEILING = 8.0

REQUEST = b"GET / HTTP/1.1\r\nHost: probe\r\n\r\n"


class SetupError(RuntimeError):
    """The server could not be started; not a finding about the keep-alive."""


def _free_port() -> int:
    """An ephemeral port on 127.0.0.1 that nothing listens on."""
    for _ in range(20):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        if port in AVOID_PORTS or port < 1024:
            continue
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as other:
            other.settimeout(0.5)
            if other.connect_ex(("127.0.0.1", port)) == 0:
                continue
        return port
    raise SetupError("no free ephemeral port on 127.0.0.1")


def _on_loopback(arguments: List[str], port: int) -> List[str]:
    """*arguments* with the stub app in place of the API and the server on
    127.0.0.1:*port*; everything else as the entrypoint built it."""
    swapped = list(arguments)
    assert swapped[0] == "backend.app.main:app", swapped
    swapped[0] = f"{STUB_MODULE}:app"
    for option, value in (("--host", "127.0.0.1"), ("--port", str(port))):
        at = swapped.index(option)
        swapped[at + 1] = value
    return swapped


def _assert_safe(arguments: List[str], port: int) -> None:
    """Before anything is started: loopback only, the stub app, a port that
    is ephemeral and not one of the repository's own."""
    assert arguments[0] == f"{STUB_MODULE}:app", arguments
    pairs = list(zip(arguments, arguments[1:]))
    assert [v for o, v in pairs if o == "--host"] == ["127.0.0.1"], arguments
    assert [v for o, v in pairs if o == "--port"] == [str(port)], arguments
    for argument in arguments:
        assert not argument.startswith(("--host=", "--port=", "--uds", "--fd")), (
            arguments
        )
    assert port != 8000 and port not in AVOID_PORTS and port >= 1024, port


def _read_response(sock: socket.socket) -> bytes:
    """One response, read up to the end of its body (Content-Length)."""
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError(f"the connection closed mid-response: {data!r}")
        data += chunk
    head, _, body = data.partition(b"\r\n\r\n")
    length = next(
        int(line.split(b":", 1)[1])
        for line in head.split(b"\r\n")
        if line.lower().startswith(b"content-length:")
    )
    while len(body) < length:
        chunk = sock.recv(4096)
        if not chunk:
            raise AssertionError(f"the connection closed mid-body: {data!r}")
        body += chunk
    return head + b"\r\n\r\n" + body


def _stub_app_answers(port: int, marker: bytes) -> bool:
    """Whether the stub app holding *marker* answers on 127.0.0.1:*port*.

    A connection that is accepted is not enough: another process may have
    taken the port between the pick and the bind, and it would accept this
    one while uvicorn is still starting. *marker* is new for each fixture, so
    another run's stub app is not ours either. Bounded to a second or two,
    whatever the other end does.
    """
    deadline = time.monotonic() + 1
    data = b""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as probe:
            probe.sendall(REQUEST)
            while marker not in data and len(data) < 4096:
                probe.settimeout(max(0.05, deadline - time.monotonic()))
                chunk = probe.recv(4096)
                if not chunk:
                    break
                data += chunk
                if time.monotonic() > deadline:
                    break
    except OSError:
        return False
    return data.startswith(b"HTTP/1.1 200 ") and data.endswith(marker)


def _stop(server: subprocess.Popen) -> None:
    """Terminate, wait, and kill only if it is still alive."""
    if server.poll() is None:
        server.terminate()
        try:
            server.wait(timeout=15)
        except subprocess.TimeoutExpired:
            server.kill()
            server.wait(timeout=15)


@pytest.fixture
def served(tmp_path):
    """The pinned uvicorn, started with the arguments the entrypoint builds
    from the image's command, on 127.0.0.1: (arguments, port, marker).

    It is ready when the stub app itself has answered with this start's
    marker, so a process that took the port between the pick and uvicorn's
    bind is never taken for it, whatever it answers. A port uvicorn cannot
    bind is picked again, five picks in all; every other failed start, and
    running out of picks, is a setup error, not a test failure.
    """
    code, stderr, started = start(tmp_path / "entrypoint", *image_command())
    if code != 0 or started is None:
        raise SetupError(f"the entrypoint did not start uvicorn ({code}):\n{stderr}")
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    marker = secrets.token_hex(16).encode("ascii")
    (app_dir / f"{STUB_MODULE}.py").write_text(_stub_app(marker))
    env = {k: v for k, v in os.environ.items() if not k.startswith("UVICORN_")}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    for attempt in range(5):
        port = _free_port()
        arguments = _on_loopback(started, port)
        _assert_safe(arguments, port)
        log_path = tmp_path / f"server-{attempt}.log"
        with log_path.open("wb") as log:
            server = subprocess.Popen(
                [sys.executable, "-m", "uvicorn", *arguments],
                cwd=app_dir,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 30
            ready = False
            while time.monotonic() < deadline and server.poll() is None:
                if not _stub_app_answers(port, marker):
                    time.sleep(0.1)
                    continue
                ready = server.poll() is None
                break
            if ready:
                yield arguments, port, marker
                return
            output = log_path.read_text(errors="replace")
            if server.poll() is not None and "address already in use" in output.lower():
                continue
            raise SetupError(
                f"uvicorn was not ready on 127.0.0.1:{port} "
                f"(exit {server.poll()}):\n{output[-3000:]}"
            )
        finally:
            _stop(server)
    raise SetupError("every port picked for uvicorn was taken before it bound")


@pytest.mark.regression
def test_a_port_taken_between_the_pick_and_the_bind_is_picked_again(
    request, monkeypatch
):
    """A listener that took the first pick accepts connections, and is not the
    stub app: the server that starts is on a later pick."""
    pick = _free_port
    tried: List[int] = []
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as taken:

        def take_the_first():
            tried.append(pick())
            if len(tried) == 1:
                taken.bind(("127.0.0.1", tried[0]))
                taken.listen()
            return tried[-1]

        monkeypatch.setattr(sys.modules[__name__], "_free_port", take_the_first)
        _, port, _ = request.getfixturevalue("served")
        assert port != tried[0] and port == tried[-1], (tried, port)


@pytest.mark.regression
def test_the_pinned_uvicorn_keeps_the_idle_connection_past_its_own_default(served):
    """V2: one request, then idle. With the flag the entrypoint adds, the
    server does not close the connection within CEILING (it would at 65 s),
    and answers a second request on it; with uvicorn's default it closes it
    at 5 s. The wait is one-sided: a slow machine can only make the server
    look better, never fail a correct entrypoint."""
    arguments, port, marker = served
    with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
        sock.sendall(REQUEST)
        first = _read_response(sock)
        assert first.startswith(b"HTTP/1.1 200 ") and first.endswith(marker), first
        idle_from = time.monotonic()
        sock.settimeout(CEILING)
        try:
            unexpected = sock.recv(1)
        except socket.timeout:
            pass
        except ConnectionResetError:
            pytest.fail(
                "the server reset the idle connection after "
                f"{time.monotonic() - idle_from:.2f}s; started with {arguments}"
            )
        else:
            assert unexpected, (
                "the server closed the idle connection: FIN after "
                f"{time.monotonic() - idle_from:.2f}s, inside the {CEILING:.0f}s "
                f"ceiling; started with {arguments}"
            )
            pytest.fail(f"the server sent bytes nobody asked for: {unexpected!r}")
        sock.settimeout(10)
        sock.sendall(REQUEST)
        second = _read_response(sock)
        assert second.startswith(b"HTTP/1.1 200 ") and second.endswith(marker), second
