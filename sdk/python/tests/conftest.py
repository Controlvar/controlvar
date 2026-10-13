"""Make ``experimentation`` importable from the source tree without installing the package.

And: no test can reach real AWS credentials or the real ``aws`` binary (below).
"""

import os
import shutil
import socket
import stat
import sys
import tempfile
from pathlib import Path

import pytest

SDK_ROOT = Path(__file__).resolve().parents[1]
if str(SDK_ROOT) not in sys.path:
    sys.path.insert(0, str(SDK_ROOT))


# ---------------------------------------------------------------------------
# No test can reach real AWS credentials or the real ``aws`` binary.
#
# A copy of backend/tests/no_real_aws.py (read that for the why). This suite
# has its own rootdir (the pyproject.toml beside it), so the repository-root
# conftest.py that loads that plugin never runs here, and the SDK does not
# import the backend. sdk/openfeature-python/tests/conftest.py carries the same
# copy; backend/tests/unit/test_no_real_aws.py fails if either drifts.
# ---------------------------------------------------------------------------
NO_REAL_AWS_MARKER = "NO-REAL-AWS"
NO_REAL_AWS_FAKE_EXIT = 97
DUMMY = {
    "AWS_ACCESS_KEY_ID": "no-real-aws-dummy",
    "AWS_SECRET_ACCESS_KEY": "no-real-aws-dummy",
    "AWS_EC2_METADATA_DISABLED": "true",
}
MISSING_FILES = ("AWS_CONFIG_FILE", "AWS_SHARED_CREDENTIALS_FILE")
UNSET = (
    "AWS_PROFILE",
    "AWS_DEFAULT_PROFILE",
    "AWS_SESSION_TOKEN",
    "AWS_SECURITY_TOKEN",
    "AWS_CREDENTIAL_EXPIRATION",
    "AWS_ROLE_ARN",
    "AWS_ROLE_SESSION_NAME",
    "AWS_WEB_IDENTITY_TOKEN_FILE",
    "AWS_CONTAINER_CREDENTIALS_RELATIVE_URI",
    "AWS_CONTAINER_CREDENTIALS_FULL_URI",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN",
    "AWS_CONTAINER_AUTHORIZATION_TOKEN_FILE",
)
_FAKE_AWS = f"""#!/bin/sh
printf '%s\\n' "$*" >> "$(dirname "$0")/calls.log"
echo "{NO_REAL_AWS_MARKER}: the aws CLI is blocked in tests (called: aws $*)." >&2
exit {NO_REAL_AWS_FAKE_EXIT}
"""
_no_real_aws_state = {}


def _isolate(mp, bin_dir):
    for name, value in DUMMY.items():
        mp.setenv(name, value)
    for name in MISSING_FILES:
        mp.setenv(name, os.path.join(bin_dir, "missing", name.lower()))
    for name in UNSET:
        mp.delenv(name, raising=False)
    path = os.environ.get("PATH", "")
    if path.split(os.pathsep, 1)[0] != bin_dir:
        mp.setenv("PATH", bin_dir + os.pathsep + path if path else bin_dir)


def pytest_configure(config):
    # This suite has its own rootdir, so the repository's marker list does not
    # reach it; a fix's test still carries the repository's regression marker.
    config.addinivalue_line("markers", "regression: a test that fails on the code before a fix")
    bin_dir = tempfile.mkdtemp(prefix="no-real-aws-")
    fake = os.path.join(bin_dir, "aws")
    with open(fake, "w") as fh:
        fh.write(_FAKE_AWS)
    os.chmod(fake, os.stat(fake).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    mp = pytest.MonkeyPatch()
    _isolate(mp, bin_dir)
    _no_real_aws_state.update(bin_dir=bin_dir, mp=mp)


def pytest_unconfigure(config):
    mp = _no_real_aws_state.pop("mp", None)
    if mp is not None:
        mp.undo()
    bin_dir = _no_real_aws_state.pop("bin_dir", None)
    if bin_dir is not None:
        shutil.rmtree(bin_dir, ignore_errors=True)


@pytest.fixture(autouse=True)
def _no_real_aws(monkeypatch):
    """Per test: dummy credentials, no ~/.aws, and a failing ``aws`` first on PATH."""
    _isolate(monkeypatch, _no_real_aws_state["bin_dir"])
    return _no_real_aws_state["bin_dir"]


# ---------------------------------------------------------------------------
# No test can open a network connection: HTTP is faked (FakeTransport, or a
# mocked ``urlopen``). A connection a test attempts is refused, and the test
# then fails at teardown even when the SDK swallowed the error (``track``
# returns ``False``), so a test that forgot to fake HTTP cannot pass by
# reaching whatever answers on localhost.
# ---------------------------------------------------------------------------
class NetworkBlocked(ConnectionRefusedError):
    """A test tried to open a real connection."""


_attempted = []


def _refuse_connection(*args, **kwargs):
    _attempted.append(next((a for a in args if isinstance(a, tuple)), args[:1]))
    raise NetworkBlocked("the SDK tests fake HTTP; a real connection was attempted")


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Yields the attempts so far; a test that makes one on purpose clears it."""
    _attempted.clear()
    monkeypatch.setattr(socket.socket, "connect", _refuse_connection)
    monkeypatch.setattr(socket.socket, "connect_ex", _refuse_connection)
    monkeypatch.setattr(socket, "create_connection", _refuse_connection)
    yield _attempted
    if _attempted:
        attempted = list(_attempted)
        _attempted.clear()
        pytest.fail(f"the test opened a real connection ({attempted}): fake HTTP with FakeTransport")
