"""The Lambda network guard (backend/tests/no_outbound_network.py, #433).

The guard exists because a Lambda test that forgot to stub boto3 stayed green
while it sent requests to the real AWS endpoint: the code under test caught the
connection error. So what is proved here is that an attempt fails the test even
when the code swallows it -- with ``except Exception``, with ``except
BaseException``, and through a real boto3 client with its retries -- that it
fails at once rather than after botocore's back-off, and that loopback still
works.

Each case runs a real pytest session in a subprocess over probe files, so what
is asserted is what a CI job would see. The probe directory is not under
``backend/lambda``, so its conftest adds itself to ``GUARDED_DIRS`` inside that
subprocess only.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.tests import no_outbound_network as guard

REPO_ROOT = Path(__file__).resolve().parents[3]

# 192.0.2.0/24 is TEST-NET-1 (RFC 5737): never routed, so even a broken guard
# cannot reach anything real with it.
CONFTEST = """
from pathlib import Path

import backend.tests.no_outbound_network as guard

guard.GUARDED_DIRS = guard.GUARDED_DIRS + (Path(__file__).resolve().parent,)
"""

PROBE = """
import os
import socket
import tempfile

import pytest


def test_swallowed_by_except_exception():
    try:
        socket.create_connection(("192.0.2.1", 443), timeout=1)
    except Exception:
        pass


def test_swallowed_by_except_base_exception():
    try:
        socket.create_connection(("192.0.2.1", 443), timeout=1)
    except BaseException:
        pass


def test_boto3_put_object_swallowed():
    boto3 = pytest.importorskip("boto3")
    from botocore.config import Config

    # Short timeouts and no retries, so that with the guard broken this
    # fails in about a second instead of hanging the outer session.
    s3 = boto3.client(
        "s3",
        region_name="us-east-1",
        endpoint_url="https://192.0.2.1",
        config=Config(connect_timeout=1, retries={"max_attempts": 1}),
    )
    try:
        s3.put_object(Bucket="no-outbound-network-probe", Key="k", Body=b"x")
    except Exception:
        pass


def test_loopback_is_allowed():
    server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    server.bind(("127.0.0.1", 0))
    server.listen(1)
    try:
        client = socket.create_connection(server.getsockname(), timeout=5)
        client.close()
    finally:
        server.close()


def test_unix_socket_is_allowed():
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("no AF_UNIX")
    # A short path: macOS refuses an AF_UNIX path over 104 bytes.
    path = os.path.join(tempfile.mkdtemp(prefix="nob-", dir="/tmp"), "s")
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(path)
    server.listen(1)
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.connect(path)
        client.close()
    finally:
        server.close()
"""


@pytest.fixture(scope="module")
def session(tmp_path_factory):
    probe_dir = tmp_path_factory.mktemp("guarded")
    (probe_dir / "conftest.py").write_text(CONFTEST)
    (probe_dir / "test_probe.py").write_text(PROBE)
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWS_")}
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-p",
            "backend.tests.no_real_aws",
            "-p",
            "backend.tests.no_outbound_network",
            "-p",
            "no:cacheprovider",
            "-p",
            "no:cov",
            "--rootdir",
            str(probe_dir),
            "-c",
            os.devnull,
            "-rA",
            "-o",
            "log_cli=false",
            str(probe_dir / "test_probe.py"),
        ],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )


def _outcomes(stdout: str, name: str) -> set[str]:
    return {
        line.split(" ", 1)[0]
        for line in stdout.splitlines()
        if line.endswith(f"::{name}") or f"::{name} - " in line
    }


@pytest.mark.regression
@pytest.mark.parametrize(
    "name",
    [
        "test_swallowed_by_except_exception",
        "test_swallowed_by_except_base_exception",
        "test_boto3_put_object_swallowed",
    ],
)
def test_an_outbound_connection_fails_the_test_even_when_swallowed(session, name):
    result = session
    # FAILED when the BaseException reaches pytest; ERROR (at teardown) when
    # the recorded attempt fails it -- code that catches BaseException passes
    # the call phase and still fails the test there.
    outcomes = _outcomes(result.stdout, name)
    assert outcomes & {"FAILED", "ERROR"}, result.stdout + result.stderr
    assert "no_outbound_network" in result.stdout
    assert "192.0.2.1" in result.stdout


@pytest.mark.parametrize(
    "name", ["test_loopback_is_allowed", "test_unix_socket_is_allowed"]
)
def test_loopback_and_unix_sockets_are_allowed(session, name):
    result = session
    assert _outcomes(result.stdout, name) <= {"PASSED", "SKIPPED"}, result.stdout
    assert _outcomes(result.stdout, name), result.stdout


def test_the_session_exits_non_zero(session):
    result = session
    assert result.returncode == 1, result.stdout + result.stderr


@pytest.mark.regression
def test_boto3_stops_at_the_first_attempt(session):
    """Raised as an ``OSError``, the guard let botocore retry with back-off (the
    event processor suite took over three minutes to fail, 120 attempts per
    test). A ``BaseException`` is not retried: one attempt, then the failure.
    The count is asserted rather than a duration."""
    result = session
    lines = result.stdout.splitlines()
    header = [
        i
        for i, line in enumerate(lines)
        if "ERROR at teardown of test_boto3_put_object_swallowed" in line
    ]
    assert header, result.stdout
    assert "attempted 1 outbound connection(s)" in lines[header[0] + 1], result.stdout


@pytest.mark.parametrize(
    ("relative", "expected"),
    [
        ("backend/lambda/event_processor/tests/test_lambda_handler.py", True),
        ("backend/lambda/shared/tests/test_utils.py", True),
        ("modules/lambda/example/tests/test_x.py", True),
        ("backend/tests/unit/showcase/test_showcase_contract.py", True),
        ("backend/tests/unit/showcase/capture_dirs.py", True),
        ("backend/tests/unit/test_no_outbound_network.py", False),
        ("backend/tests/unit/showcase_other/test_x.py", False),
        ("modules/backend/tests/test_x.py", False),
    ],
)
def test_only_the_lambda_and_showcase_directories_are_guarded(relative, expected):
    assert guard.is_guarded(REPO_ROOT / relative) is expected


def test_each_guarded_directory_exists():
    """A directory renamed away would leave the guard covering nothing, silently.

    ``modules/lambda`` is absent from a core build, so it is not asked for.
    """
    missing = [
        str(d.relative_to(REPO_ROOT))
        for d in guard.GUARDED_DIRS
        if d.parts[-2] != "modules" and not d.is_dir()
    ]
    assert missing == []


def test_a_lambda_directory_elsewhere_is_not_guarded(tmp_path):
    assert (
        guard.is_guarded(tmp_path / "backend" / "lambda" / "x" / "test_x.py") is False
    )


@pytest.mark.parametrize(
    ("host", "expected"),
    [
        ("127.0.0.1", True),
        ("127.8.9.10", True),
        ("::1", True),
        ("localhost", True),
        ("LOCALHOST", True),
        ("192.0.2.1", False),
        ("10.0.0.1", False),
        ("s3.amazonaws.com", False),
        ("localhost.evil.example", False),
        ("", False),
        (None, False),
    ],
)
def test_is_loopback(host, expected):
    assert guard.is_loopback(host) is expected


@pytest.mark.regression
def test_the_root_conftest_loads_it():
    """Every probe above loads the plugin with ``-p``; this is what makes the
    Lambda suites load it. Without it they would pass while reaching AWS."""
    import ast

    tree = ast.parse((REPO_ROOT / "conftest.py").read_text())
    plugins = [
        ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(t, "id", None) == "pytest_plugins" for t in node.targets)
    ]
    assert plugins and "backend.tests.no_outbound_network" in plugins[0]
