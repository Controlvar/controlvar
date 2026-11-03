"""The driver runner against fake drivers (``python -c`` scripts): a round
trip, and every way a driver can break the protocol (a hang, an exit before
the answer, an answer that is not JSON, too long, or without ``ok``), each of
which must kill and reap the driver and raise an error that names only the
command's verb. Then a driver flooding stderr, and secrets: an echoed key is
redacted, and canaries placed in a request's query string, its ``X-API-Key``
and ``init``'s key reach no record, no output and no error.

The hang and stderr gates inject timeouts of at most 1 s. The one bound on
time here is that a hung driver's call returns, the driver killed and reaped,
within a second of an injected 0.2 s timeout.
"""

from __future__ import annotations

import http.client
import signal
import sys
import threading
import traceback
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from backend.tests.unit.usecases.harness import FLAG_KEY, Upstream, proxy, runner

pytestmark = pytest.mark.unit

#: Stand-ins with no entropy, so no secret scanner mistakes them for one.
KEY = "K" * 32
QUERY_CANARY = "canary" + "Q" * 20
HEADER_CANARY = "canary" + "H" * 20
INIT_CANARY = "canary" + "I" * 20
CANARIES = (QUERY_CANARY, HEADER_CANARY, INIT_CANARY)

_READ = "import json, sys\nfor line in sys.stdin:\n    command = json.loads(line)\n"
#: Answers every command with its verb; exits after ``close``.
ECHO = (
    _READ
    + "    sys.stdout.write(json.dumps({'ok': True, 'verb': command['cmd']}) + '\\n')\n"
    + "    sys.stdout.flush()\n"
    + "    if command['cmd'] == 'close':\n        break\n"
)
#: Reads one command and never answers.
HANG = "import sys, time\nsys.stdin.readline()\ntime.sleep(3600)\n"
#: Reads one command and exits 4.
EXIT_EARLY = "import sys\nsys.stdin.readline()\nsys.exit(4)\n"
#: Writes the command back, not as JSON (so it carries the key).
NOT_JSON = (
    "import sys\nfor line in sys.stdin:\n"
    "    sys.stdout.write('not json: ' + line)\n    sys.stdout.flush()\n"
)
#: Answers a JSON value with no boolean ``ok``.
NO_OK = (
    "import sys\nsys.stdin.readline()\nsys.stdout.write('[1]\\n')\nsys.stdout.flush()\n"
)
#: Answers one line of 70 KiB.
TOO_LONG = (
    "import sys\nsys.stdin.readline()\n"
    "sys.stdout.write('{\"ok\": true, \"x\": \"' + 'x' * 71680 + '\"}\\n')\n"
    "sys.stdout.flush()\nimport time\ntime.sleep(3600)\n"
)
#: Writes 256 KiB to stderr before each answer.
STDERR_FLOOD = (
    _READ
    + "    sys.stderr.write('e' * 262144)\n    sys.stderr.flush()\n"
    + "    sys.stdout.write(json.dumps({'ok': True}) + '\\n')\n    sys.stdout.flush()\n"
    + "    if command['cmd'] == 'close':\n        break\n"
)
#: Writes the init command's key back inside its answer.
ECHO_KEY = (
    _READ
    + "    sys.stdout.write(json.dumps({'ok': True, 'echo': command.get('api_key')}) + '\\n')\n"
    + "    sys.stdout.flush()\n"
)


def _driver(script: str, **kwargs: Any) -> runner.Runner:
    return runner.Runner([sys.executable, "-B", "-c", script], **kwargs)


@pytest.fixture
def started() -> Iterator[Callable[..., runner.Runner]]:
    made: list[runner.Runner] = []

    def start(script: str, **kwargs: Any) -> runner.Runner:
        driver = _driver(script, **kwargs).start()
        made.append(driver)
        return driver

    yield start
    for driver in made:
        driver.stop()


def _bounded(call: Callable[[], Any], driver: runner.Runner, seconds: float) -> Any:
    """``call()``'s exception (or result); fails, and kills the driver, if it
    has not returned within *seconds*: a hang must be a red, not a hung run."""
    outcome: list[Any] = []

    def run() -> None:
        try:
            outcome.append(call())
        except BaseException as exc:
            outcome.append(exc)

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=seconds)
    if thread.is_alive():
        process = driver._process
        if process is not None:
            process.kill()
            process.wait(timeout=5)
        pytest.fail(f"the call did not return within {seconds} s")
    return outcome[0]


# -- the protocol ------------------------------------------------------------------
def test_a_command_round_trips_and_close_ends_the_driver(started):
    driver = started(ECHO, timeout=5.0)
    assert driver.call("hello") == {"ok": True, "verb": "hello"}
    assert driver.call("close") == {"ok": True, "verb": "close"}
    driver.stop()
    assert driver.returncode == 0


def test_a_verb_outside_the_protocol_is_refused_before_anything_is_sent(started):
    driver = started(ECHO, timeout=5.0)
    with pytest.raises(ValueError, match="not a command of the protocol"):
        driver.call("assign")
    assert driver.call("hello")["verb"] == "hello"


def test_a_hung_driver_is_killed_and_reaped_within_the_injected_timeout(started):
    driver = started(HANG, timeout=0.2)
    error = _bounded(lambda: driver.call("hello"), driver, seconds=1.0)
    assert isinstance(error, runner.DriverError), error
    assert "did not answer hello within 0.2 s (hung)" in str(error)
    assert driver.returncode == -signal.SIGKILL


def test_a_driver_that_exits_before_answering_fails_loudly(started):
    driver = started(EXIT_EARLY, timeout=5.0)
    with pytest.raises(runner.DriverError, match=r"exited before answering init"):
        driver.call("init", api_url="http://127.0.0.1:1", api_key=KEY, mode="server")
    assert driver.returncode == 4


@pytest.mark.parametrize(
    "script, message",
    [
        (NOT_JSON, "answered status with a line that is not JSON"),
        (NO_OK, "answered status with no boolean 'ok'"),
        (TOO_LONG, "answer to status is longer than 65536 bytes"),
    ],
    ids=["not-json", "no-ok", "too-long"],
)
def test_a_broken_answer_kills_and_reaps_the_driver(started, script, message):
    driver = started(script, timeout=5.0)
    with pytest.raises(runner.DriverError, match=message):
        driver.call("status")
    assert driver.returncode is not None


def test_a_driver_writing_256_kib_to_stderr_still_round_trips(started, capfd):
    driver = started(STDERR_FLOOD, timeout=1.0)
    assert driver.call("hello") == {"ok": True}
    assert driver.call("close") == {"ok": True}
    driver.stop()
    assert driver.returncode == 0
    assert driver.stderr_bytes == 2 * 262144
    # Drained, never shown.
    out, err = capfd.readouterr()
    assert "e" * 1024 not in out + err


def test_a_key_the_driver_writes_back_is_redacted(started):
    driver = started(ECHO_KEY, timeout=5.0)
    answer = driver.call(
        "init", api_url="http://127.0.0.1:1", api_key=KEY, mode="server"
    )
    assert answer == {"ok": True, "echo": runner.REDACTED}


# -- canaries: the query string, the X-API-Key header and init's key -----------------
@pytest.mark.parametrize(
    "script, timeout, watchdog, failure",
    [(HANG, 0.2, 1.0, "(hung)"), (NOT_JSON, 5.0, 10.0, "not JSON")],
    ids=["hang", "not-json"],
)
def test_canaries_reach_no_record_no_output_and_no_error(
    started, capfd, monkeypatch, script, timeout, watchdog, failure
):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    upstream = Upstream().start()
    try:
        with proxy.RecordingProxy(upstream.origin) as relay:
            host, port = relay.server_address
            connection = http.client.HTTPConnection(host, port, timeout=5)
            try:
                connection.request(
                    "GET",
                    f"/api/v1/feature-flags/evaluate/{FLAG_KEY}?user_id={QUERY_CANARY}",
                    headers={"X-API-Key": HEADER_CANARY},
                )
                connection.getresponse().read()
            finally:
                connection.close()
            driver = started(script, timeout=timeout, secrets=[HEADER_CANARY])
            error = _bounded(
                lambda: driver.call(
                    "init", api_url=relay.origin, api_key=INIT_CANARY, mode="server"
                ),
                driver,
                seconds=watchdog,
            )
            records = relay.records()
    finally:
        upstream.stop()
    # The canaries did reach the upstream: the proxy forwards them.
    (received,) = upstream.received
    assert QUERY_CANARY in received.target
    assert received.headers["x-api-key"] == HEADER_CANARY
    assert isinstance(error, runner.DriverError), error
    assert failure in str(error)
    assert driver.returncode is not None
    shown = {
        "records": repr(records),
        "error": str(error) + repr(error) + "".join(traceback.format_exception(error)),
        "output": "".join(capfd.readouterr()),
    }
    leaked = [
        (where, canary)
        for where, text in shown.items()
        for canary in CANARIES
        if canary in text
    ]
    assert leaked == []
