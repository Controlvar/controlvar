"""No Lambda or showcase test may open a network connection to anything but loopback.

A pytest plugin, loaded for every session rooted at the repository root by
``conftest.py`` there (``pytest_plugins``), like ``no_real_aws``. Its fixture
is autouse but acts only on tests whose file lies under ``backend/lambda/``,
``modules/lambda/`` or ``backend/tests/unit/showcase/``: every Lambda suite,
including one added later, is covered without its conftest having to opt in --
and the Lambda packages could not import it anyway, since
``backend/lambda/.importlinter`` forbids them to import ``backend``. The
showcase tests (#1066) cover a tool that starts a browser and a local stack;
they check its logic against stand-ins and must not reach any network service
themselves.

Why: the Lambda functions build real boto3 clients on a cold start. A test
that forgets to stub one of them does not fail. ``no_real_aws`` gives it dummy
credentials, the request goes to the real AWS endpoint, AWS answers
``InvalidAccessKeyId``, and the handler under test catches that and carries on
(#433: the event processor's handler tests sent ``PutObject`` to S3 on every
run). The test stays green; it is merely slower, and depends on the network.

What it does, for the duration of each guarded test: ``socket.socket.connect``
and ``connect_ex`` refuse an ``AF_INET``/``AF_INET6`` address that is not
loopback. The call raises :class:`OutboundNetworkBlocked`, so nothing leaves
the machine and the test fails there and then; it is a ``BaseException`` so
that the ``except Exception`` and retry loops in the code under test do not
absorb it. The attempt is also recorded and the test fails at teardown with
the address it tried to reach, so even code that catches ``BaseException``
cannot turn it into a pass.

Loopback stays allowed (moto's server mode, LocalStack on 127.0.0.1, the
showcase tests' own local listeners), as do Unix-domain sockets. The rest of
``backend/tests`` is deliberately not covered: its integration tests reach
PostgreSQL and Redis by service host name in CI.

What it cannot see: a connection made below Python's ``socket`` module (a C
library such as libpq opening its own socket), a DNS lookup (it blocks the
connect that follows, not the ``getaddrinfo``), and a subprocess.

It must stay importable with only pytest installed (tests/sdk-contract loads
the root conftest that way).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
GUARDED_DIRS = (
    _REPO_ROOT / "backend" / "lambda",
    _REPO_ROOT / "modules" / "lambda",
    _REPO_ROOT / "backend" / "tests" / "unit" / "showcase",
)

_LOOPBACK_NAMES = {"localhost", "localhost.localdomain", "ip6-localhost"}


class OutboundNetworkBlocked(BaseException):
    """Raised in place of a connection to a non-loopback address.

    A ``BaseException``, not an ``OSError``: urllib3 and botocore turn an
    ``OSError`` from ``connect`` into a retryable ``EndpointConnectionError``,
    and the Lambda code wraps its AWS calls in ``except Exception`` with
    retries and sleeps of its own. Raised as an ``OSError`` the guard took the
    event processor's suite from seconds to over three minutes before it
    failed; as a ``BaseException`` it passes through all of that and fails the
    test at the first attempt.
    """


def is_loopback(host: object) -> bool:
    """True for a loopback address or name; False for anything else."""
    if not isinstance(host, str):
        return False
    if host.lower() in _LOOPBACK_NAMES:
        return True
    try:
        # An IPv6 address may carry a zone ("fe80::1%en0").
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def is_guarded(path: Path) -> bool:
    """True when a test file lies under one of :data:`GUARDED_DIRS`."""
    resolved = path.resolve()
    return any(resolved.is_relative_to(root) for root in GUARDED_DIRS)


def install_guard(monkeypatch: pytest.MonkeyPatch, attempts: list[str]) -> None:
    """Refuse non-loopback connects, appending each refused address to ``attempts``."""
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex

    def _check(sock: socket.socket, address: object) -> None:
        if sock.family not in (socket.AF_INET, socket.AF_INET6):
            return
        host = address[0] if isinstance(address, tuple) and address else address
        if is_loopback(host):
            return
        attempts.append(repr(address))
        raise OutboundNetworkBlocked(
            f"no_outbound_network: a guarded test tried to connect to {address!r}. "
            "Tests must not reach AWS or any other network service; stub the "
            "client (unittest.mock or moto) instead."
        )

    def guarded_connect(self: socket.socket, address: object) -> None:
        _check(self, address)
        return real_connect(self, address)

    def guarded_connect_ex(self: socket.socket, address: object) -> int:
        _check(self, address)
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)


@pytest.fixture(autouse=True)
def _no_outbound_network(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch
) -> Iterator[list[str]]:
    """Per guarded test: refuse and record every non-loopback connection.

    Yields the list of refused addresses; the test fails at teardown if it is
    not empty. Outside the guarded directories it does nothing.
    """
    attempts: list[str] = []
    if is_guarded(Path(str(request.node.path))):
        install_guard(monkeypatch, attempts)
    yield attempts
    if attempts:
        distinct = sorted(set(attempts))
        shown = ", ".join(distinct[:5]) + (", ..." if len(distinct) > 5 else "")
        pytest.fail(
            "no_outbound_network: this test attempted "
            f"{len(attempts)} outbound connection(s), to {shown}. "
            "The code under test may have caught the error, but a guarded test "
            "must not reach the network at all.",
            pytrace=False,
        )
