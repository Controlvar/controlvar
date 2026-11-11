"""boto3 clients reach only the fixed regional endpoints, on public addresses.

No test here can reach AWS: ``socket.getaddrinfo`` is replaced for the
duration of each test, so every name -- in the hook and in botocore's own
HTTP layer -- resolves to the answer the test chose, and the credentials are
dummies.
"""

from __future__ import annotations

import logging
import socket
import threading
import time
from datetime import datetime, timezone
from urllib.parse import parse_qs, urlsplit

import boto3
import pytest
from botocore.exceptions import EndpointConnectionError, NoCredentialsError

from modules.backend.app.warehouse import aws as make_client_module
from modules.backend.app.warehouse.aws import (
    AWS_REGIONS,
    BeforeSendGuard,
    aws_endpoint_host,
    boto_config,
    make_client,
)
from modules.backend.app.warehouse.aws import call as aws_call
from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.app.warehouse.errors import (
    MESSAGES,
    WarehouseError,
    WarehouseErrorCode,
)
from modules.backend.tests.unit.warehouse.conftest import FakeClock

REFUSED = WarehouseErrorCode.DESTINATION_NOT_ALLOWED


@pytest.fixture
def aws_env(monkeypatch):
    for name in (
        "AWS_PROFILE",
        "AWS_SESSION_TOKEN",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_STS",
        "AWS_USE_FIPS_ENDPOINT",
        "AWS_USE_DUALSTACK_ENDPOINT",
        "HTTPS_PROXY",
        "https_proxy",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_EC2_METADATA_DISABLED", "true")


def _session():
    return boto3.session.Session(
        aws_access_key_id="AKIDUMMYDUMMYDUMMY00",
        aws_secret_access_key="dummy-secret",
        region_name="us-east-1",
    )


def _resolve_everything_to(monkeypatch, address: str, port: int) -> list:
    """Every lookup answers ``address:port``; returns the list of names looked up."""
    looked_up = []
    family = socket.AF_INET6 if ":" in address else socket.AF_INET

    def fake_getaddrinfo(host, service, *args, **kwargs):
        looked_up.append(host)
        sockaddr = (
            (address, port, 0, 0) if family == socket.AF_INET6 else (address, port)
        )
        return [(family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", sockaddr)]

    monkeypatch.setattr(socket, "getaddrinfo", fake_getaddrinfo)
    return looked_up


@pytest.mark.parametrize("address", ["127.0.0.1", "10.1.2.3", "169.254.169.254"])
def test_boto_before_send_refuses_private(address, probe, aws_env, monkeypatch):
    """The STS endpoint name resolving to a private address: the call is refused
    before botocore sends it, and (for loopback) the probe accepts nothing."""
    port = probe.port if address == "127.0.0.1" else 443
    looked_up = _resolve_everything_to(monkeypatch, address, port)
    client = make_client(_session(), "sts", "us-east-1", warehouse="athena")
    try:
        client.get_caller_identity()
        outcome = "sent"
    except WarehouseError as err:
        outcome = err.code
    except Exception as exc:  # botocore's own failure, once it has sent
        outcome = type(exc).__name__
    for _ in range(10):
        if probe.accepted:
            break
        time.sleep(0.02)
    assert probe.accepted == 0
    assert outcome is REFUSED
    assert looked_up and set(looked_up) == {"sts.us-east-1.amazonaws.com"}


def test_boto_before_send_lets_a_public_address_through(aws_env, monkeypatch):
    """Positive control: a public answer passes the hook (the send is then stopped
    by a later handler, so nothing leaves the machine)."""
    _resolve_everything_to(monkeypatch, "93.184.216.34", 443)
    client = make_client(_session(), "sts", "us-east-1", warehouse="athena")
    seen = []

    class Stop(Exception):
        pass

    def stop(request, **kwargs):
        seen.append(request.url)
        raise Stop()

    client.meta.events.register("before-send", stop)
    with pytest.raises(Stop):
        client.get_caller_identity()
    assert seen == ["https://sts.us-east-1.amazonaws.com/"]


def test_boto_hook_refuses_another_host():
    guard = BeforeSendGuard(
        "sts.us-east-1.amazonaws.com",
        warehouse="athena",
        resolver=lambda h, p: [("93.184.216.34", p)],
    )

    class Request:
        url = "https://sts.us-east-1.amazonaws.com.example.com/"

    with pytest.raises(WarehouseError) as err:
        guard(Request())
    assert err.value.code is REFUSED


def test_boto_hook_refuses_after_the_deadline():
    clock = FakeClock()
    deadline = Deadline(10, clock=clock)
    guard = BeforeSendGuard(
        "athena.us-east-1.amazonaws.com",
        warehouse="athena",
        deadline=deadline,
        resolver=lambda h, p: [("93.184.216.34", p)],
    )

    class Request:
        url = "https://athena.us-east-1.amazonaws.com/"

    guard(Request())
    clock.advance(10)
    with pytest.raises(WarehouseError) as err:
        guard(Request())
    assert err.value.code is WarehouseErrorCode.TIME_LIMIT


@pytest.mark.parametrize(
    "service,region",
    [
        ("s3", "us-east-1"),
        ("sts", "cn-north-1"),
        ("athena", "us-gov-west-1"),
        ("glue", "local"),
    ],
)
def test_only_fixed_services_and_regions(service, region, aws_env):
    with pytest.raises(WarehouseError) as err:
        make_client(_session(), service, region, warehouse="athena")
    assert err.value.code is REFUSED


def test_a_configured_endpoint_override_is_refused(aws_env, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://127.0.0.1:4566")
    with pytest.raises(WarehouseError) as err:
        make_client(_session(), "sts", "us-east-1", warehouse="athena")
    assert err.value.code is REFUSED


def test_boto_config_has_fixed_limits_and_no_proxies():
    config = boto_config()
    assert config.connect_timeout == 5
    assert config.read_timeout == 30
    assert config.retries == {"total_max_attempts": 2, "mode": "standard"}
    assert config.proxies == {}


class _SilentServer:
    """Accepts TCP connections on 127.0.0.1 and never sends a byte."""

    def __init__(self) -> None:
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self._sock.settimeout(0.2)
        self.port = self._sock.getsockname()[1]
        self.held = []
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self._sock.accept()
            except (socket.timeout, OSError):
                continue
            self.held.append(conn)

    def close(self) -> None:
        self._stop.set()
        self._thread.join(2)
        for conn in self.held:
            conn.close()
        self._sock.close()


def test_boto_limits_follow_the_deadline():
    """With 2 s left: connect and read limits of 2 s and a single attempt; with
    plenty left, the fixed 5 s / 30 s and two attempts; with none, time_limit."""
    clock = FakeClock()
    deadline = Deadline(100, clock=clock)
    roomy = boto_config(deadline)
    assert (roomy.connect_timeout, roomy.read_timeout) == (5, 30)
    assert roomy.retries == {"total_max_attempts": 2, "mode": "standard"}
    clock.advance(98)
    tight = boto_config(deadline)
    assert tight.connect_timeout == pytest.approx(2)
    assert tight.read_timeout == pytest.approx(2)
    assert tight.retries == {"total_max_attempts": 1, "mode": "standard"}
    clock.advance(2)
    with pytest.raises(WarehouseError) as err:
        boto_config(deadline)
    assert err.value.code is WarehouseErrorCode.TIME_LIMIT


def test_boto_call_with_two_seconds_left_is_bounded_by_them(aws_env, monkeypatch):
    """A call with 2 s left on the deadline gets 2 s limits and one attempt.

    The deadline's clock is fake (it says 2 s are left).  The call really
    runs: every name resolves to a local listener that accepts and never
    answers, and the address check is given a public answer so the call
    reaches botocore's send.  What is asserted is deterministic: the limits
    botocore applied to the socket (every ``settimeout`` on it) are
    ``min(fixed, remaining)`` = 2 s, and exactly one connection was made.
    """
    applied = []
    real_settimeout = socket.socket.settimeout

    def spy_settimeout(sock, value):
        applied.append(value)
        return real_settimeout(sock, value)

    server = _SilentServer()
    try:
        _resolve_everything_to(monkeypatch, "127.0.0.1", server.port)
        clock = FakeClock()
        deadline = Deadline(60, clock=clock)
        clock.advance(58)
        monkeypatch.setattr(socket.socket, "settimeout", spy_settimeout)
        with pytest.raises(WarehouseError) as err:
            aws_call(
                _session(),
                "sts",
                "us-east-1",
                "get_caller_identity",
                warehouse="athena",
                deadline=deadline,
                resolver=lambda host, port: [("93.184.216.34", port)],
            )
        monkeypatch.setattr(socket.socket, "settimeout", real_settimeout)
    finally:
        server.close()
    assert err.value.code in (
        WarehouseErrorCode.TIME_LIMIT,
        WarehouseErrorCode.UNREACHABLE,
    )
    limits = [value for value in applied if value is not None]
    assert limits, "botocore applied no limit to the socket"
    assert all(value == pytest.approx(2.0) for value in limits), limits
    assert len(server.held) == 1, f"{len(server.held)} connections"


def test_boto_client_errors_are_coded(aws_env, monkeypatch):
    """An AWS error answer maps by its structured Code; its message is dropped."""
    from botocore.stub import Stubber

    _resolve_everything_to(monkeypatch, "93.184.216.34", 443)
    session = _session()
    real_make = make_client_module.make_client

    def stubbed(*args, **kwargs):
        client = real_make(*args, **kwargs)
        stubber = Stubber(client)
        stubber.add_client_error(
            "get_caller_identity",
            service_error_code="AccessDenied",
            service_message="not for you: MESSAGE-TEXT-91",
            http_status_code=403,
        )
        stubber.activate()
        return client

    monkeypatch.setattr(make_client_module, "make_client", stubbed)
    with pytest.raises(WarehouseError) as err:
        aws_call(
            session,
            "sts",
            "us-east-1",
            "get_caller_identity",
            warehouse="athena",
            deadline=Deadline(30),
        )
    assert err.value.code is WarehouseErrorCode.PERMISSION_DENIED
    assert err.value.vendor_code == "AccessDenied"
    assert "MESSAGE-TEXT-91" not in repr(err.value) + err.value.message
    assert err.value.__context__ is None


# -- every failure names the warehouse it was called for (#826) ---------------


class _Request:
    def __init__(self, url: str) -> None:
        self.url = url


class _Raising:
    """A client whose every operation raises ``exc`` (no request is built)."""

    def __init__(self, exc: Exception) -> None:
        self._exc = exc

    def __getattr__(self, name):
        def operation(**params):
            raise self._exc

        return operation


def _raising_client(monkeypatch, exc: Exception) -> None:
    monkeypatch.setattr(
        make_client_module, "make_client", lambda *args, **kwargs: _Raising(exc)
    )


def _host_refused(warehouse, monkeypatch):
    aws_endpoint_host("s3", "us-east-1", warehouse=warehouse)


def _guard_other_host(warehouse, monkeypatch):
    BeforeSendGuard(
        "sts.us-east-1.amazonaws.com",
        warehouse=warehouse,
        resolver=lambda h, p: [("93.184.216.34", p)],
    )(_Request("https://sts.us-east-1.amazonaws.com.example.com/"))


def _guard_no_answer(warehouse, monkeypatch):
    def fails(host, port):
        raise OSError("no answer")

    BeforeSendGuard("sts.us-east-1.amazonaws.com", warehouse=warehouse, resolver=fails)(
        _Request("https://sts.us-east-1.amazonaws.com/")
    )


def _guard_private_answer(warehouse, monkeypatch):
    BeforeSendGuard(
        "sts.us-east-1.amazonaws.com",
        warehouse=warehouse,
        resolver=lambda h, p: [("10.1.2.3", p)],
    )(_Request("https://sts.us-east-1.amazonaws.com/"))


def _endpoint_override(warehouse, monkeypatch):
    monkeypatch.setenv("AWS_ENDPOINT_URL", "https://127.0.0.1:4566")
    make_client(_session(), "sts", "us-east-1", warehouse=warehouse)


def _client_error(warehouse, monkeypatch):
    from botocore.stub import Stubber

    _resolve_everything_to(monkeypatch, "93.184.216.34", 443)
    real_make = make_client_module.make_client

    def stubbed(*args, **kwargs):
        client = real_make(*args, **kwargs)
        stubber = Stubber(client)
        stubber.add_client_error(
            "get_caller_identity",
            service_error_code="InternalFailure",
            http_status_code=500,
        )
        stubber.activate()
        return client

    monkeypatch.setattr(make_client_module, "make_client", stubbed)
    aws_call(
        _session(),
        "sts",
        "us-east-1",
        "get_caller_identity",
        warehouse=warehouse,
        deadline=Deadline(30),
    )


def _timed_out(warehouse, monkeypatch):
    from botocore.exceptions import ConnectTimeoutError

    _raising_client(monkeypatch, ConnectTimeoutError(endpoint_url="https://x/"))
    aws_call(
        _session(),
        "sts",
        "us-east-1",
        "get_caller_identity",
        warehouse=warehouse,
        deadline=Deadline(30),
    )


def _not_connected(warehouse, monkeypatch):
    _raising_client(monkeypatch, EndpointConnectionError(endpoint_url="https://x/"))
    aws_call(
        _session(),
        "sts",
        "us-east-1",
        "get_caller_identity",
        warehouse=warehouse,
        deadline=Deadline(30),
    )


#: aws.py's eight failures (on main each said warehouse="athena"), with the
#: code each one is.
FAILURE_SITES = {
    "endpoint-host": (_host_refused, REFUSED),
    "guard-other-host": (_guard_other_host, REFUSED),
    "guard-no-answer": (_guard_no_answer, WarehouseErrorCode.UNREACHABLE),
    "guard-private-answer": (_guard_private_answer, REFUSED),
    "client-endpoint": (_endpoint_override, REFUSED),
    "call-client-error": (
        _client_error,
        WarehouseErrorCode.UNRECOGNISED_WAREHOUSE_ERROR,
    ),
    "call-time-limit": (_timed_out, WarehouseErrorCode.TIME_LIMIT),
    "call-unreachable": (_not_connected, WarehouseErrorCode.UNREACHABLE),
}


@pytest.mark.regression
@pytest.mark.parametrize("site", sorted(FAILURE_SITES))
def test_every_failure_names_the_warehouse_it_was_called_for(
    site, aws_env, monkeypatch
):
    """#826: aws.py said ``warehouse="athena"`` at all eight of its failures, so
    a Redshift connection's failure would have named Amazon Athena."""
    failing, code = FAILURE_SITES[site]
    with pytest.raises(WarehouseError) as err:
        failing("redshift", monkeypatch)
    assert err.value.code is code
    assert err.value.warehouse == "redshift"
    assert err.value.log_fields()["warehouse"] == "redshift"
    assert "Athena" not in err.value.message
    if "{warehouse}" in MESSAGES[code]:
        assert "Amazon Redshift" in err.value.message


def test_every_function_requires_the_warehouse(aws_env):
    """No default: a caller that forgets says nothing, rather than "Athena"."""
    attempts = [
        lambda: aws_endpoint_host("sts", "us-east-1"),
        lambda: BeforeSendGuard("sts.us-east-1.amazonaws.com"),
        lambda: make_client(_session(), "sts", "us-east-1"),
        lambda: aws_call(
            _session(), "sts", "us-east-1", "get_caller_identity", deadline=Deadline(30)
        ),
    ]
    for attempt in attempts:
        with pytest.raises(TypeError, match="warehouse"):
            attempt()


@pytest.mark.parametrize("warehouse", ["redshfit", "bigquery", "", None])
def test_every_function_refuses_a_warehouse_that_is_not_an_aws_one(warehouse, aws_env):
    attempts = [
        lambda: aws_endpoint_host("sts", "us-east-1", warehouse=warehouse),
        lambda: BeforeSendGuard("sts.us-east-1.amazonaws.com", warehouse=warehouse),
        lambda: make_client(_session(), "sts", "us-east-1", warehouse=warehouse),
        lambda: aws_call(
            _session(),
            "sts",
            "us-east-1",
            "get_caller_identity",
            warehouse=warehouse,
            deadline=Deadline(30),
        ),
    ]
    for attempt in attempts:
        with pytest.raises(ValueError):
            attempt()


def test_the_aws_warehouses_are_athena_and_redshift():
    assert make_client_module.AWS_WAREHOUSES == frozenset({"athena", "redshift"})


# -- call()'s codes: no identity, our own parameters, a per-call table (#826) --


def _closed_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _only_loopback_resolves(monkeypatch) -> list:
    """Every name but 127.0.0.1 fails to resolve; returns the names looked up."""
    looked_up = []
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(host, *args, **kwargs):
        looked_up.append(host)
        if host != "127.0.0.1":
            raise socket.gaierror(socket.EAI_NONAME, "not in this test")
        return real_getaddrinfo(host, *args, **kwargs)

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return looked_up


@pytest.mark.regression
@pytest.mark.parametrize("metadata", ["disabled", "unreachable"])
def test_a_deployment_with_no_aws_identity_has_its_own_code(
    metadata, aws_env, monkeypatch
):
    """#826: with no AWS identity botocore raises NoCredentialsError, which
    call() reported as ``unreachable``, so the operator was told the warehouse
    was down.  Run twice: instance metadata disabled, and at a closed port."""
    for name in ("AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("BOTO_CONFIG", "/dev/null")
    if metadata == "unreachable":
        monkeypatch.delenv("AWS_EC2_METADATA_DISABLED", raising=False)
        monkeypatch.setenv(
            "AWS_EC2_METADATA_SERVICE_ENDPOINT", f"http://127.0.0.1:{_closed_port()}/"
        )
    looked_up = _only_loopback_resolves(monkeypatch)
    with pytest.raises(WarehouseError) as err:
        aws_call(
            boto3.session.Session(region_name="us-east-1"),
            "sts",
            "us-east-1",
            "get_caller_identity",
            warehouse="redshift",
            deadline=Deadline(30),
        )
    assert err.value.code is WarehouseErrorCode.AWS_IDENTITY_UNAVAILABLE
    assert err.value.status_code == 503
    assert err.value.message == (
        "This deployment has no AWS identity to assume your role from, "
        "so Amazon Redshift can't be used here."
    )
    assert "credential" not in err.value.message.lower()
    assert "arn" not in err.value.message.lower()
    # Positive control: the metadata service was asked when it was on (a token
    # request, then the fallback), and nothing was looked up when it was off;
    # no other name was ever resolved.
    if metadata == "unreachable":
        assert looked_up and set(looked_up) == {"127.0.0.1"}
    else:
        assert looked_up == []


def test_our_own_bad_parameters_are_internal_not_unreachable(aws_env, monkeypatch):
    """botocore refuses a parameter the model does not have before sending
    anything: that is our mistake, not the warehouse being down."""
    looked_up = _only_loopback_resolves(monkeypatch)
    with pytest.raises(WarehouseError) as err:
        aws_call(
            _session(),
            "sts",
            "us-east-1",
            "get_caller_identity",
            warehouse="redshift",
            deadline=Deadline(30),
            NotAParameter="x",
        )
    assert err.value.code is WarehouseErrorCode.INTERNAL
    assert err.value.warehouse == "redshift"
    assert looked_up == []


def _client_error_answer(monkeypatch, code: str, status: int) -> None:
    from botocore.stub import Stubber

    _resolve_everything_to(monkeypatch, "93.184.216.34", 443)
    real_make = make_client_module.make_client

    def stubbed(*args, **kwargs):
        client = real_make(*args, **kwargs)
        stubber = Stubber(client)
        stubber.add_client_error(
            "get_caller_identity",
            service_error_code=code,
            service_message="not for you: MESSAGE-TEXT-92",
            http_status_code=status,
        )
        stubber.activate()
        return client

    monkeypatch.setattr(make_client_module, "make_client", stubbed)


@pytest.mark.parametrize(
    "codes,expected",
    [
        (
            {"AccessDenied": WarehouseErrorCode.AUTH_FAILED},
            WarehouseErrorCode.AUTH_FAILED,
        ),
        # A table without the code: the status decides, as with no table.
        (
            {"Throttling": WarehouseErrorCode.WAREHOUSE_BUSY},
            WarehouseErrorCode.PERMISSION_DENIED,
        ),
        (None, WarehouseErrorCode.PERMISSION_DENIED),
    ],
    ids=["mapped", "not-in-the-table", "no-table"],
)
def test_a_per_call_code_table_maps_the_aws_code(codes, expected, aws_env, monkeypatch):
    _client_error_answer(monkeypatch, "AccessDenied", 403)
    with pytest.raises(WarehouseError) as err:
        aws_call(
            _session(),
            "sts",
            "us-east-1",
            "get_caller_identity",
            warehouse="redshift",
            deadline=Deadline(30),
            codes=codes,
        )
    assert err.value.code is expected
    assert err.value.vendor_code == "AccessDenied"
    assert "MESSAGE-TEXT-92" not in repr(err.value) + err.value.message


# -- assume_role_session: the customer's role, through the guard (#826) -------

ROLE_ARN = "arn:aws:iam::123456789012:role/controlvar-warehouse/analysis-reader"
EXTERNAL_ID = "exp-" + "0123456789abcdef" * 2
#: Stands in for the temporary credentials STS hands out.
TEMP_SENTINEL = "TEMPCREDSENTINEL4C1F"
ASSUMED = {
    "Credentials": {
        "AccessKeyId": "ASIA" + TEMP_SENTINEL,
        "SecretAccessKey": "secret-" + TEMP_SENTINEL,
        "SessionToken": "token-" + TEMP_SENTINEL,
        "Expiration": datetime(2026, 11, 7, 12, 15, tzinfo=timezone.utc),
    },
    "AssumedRoleUser": {
        "AssumedRoleId": "AROA0123456789ABCDEFG:controlvar-warehouse",
        "Arn": "arn:aws:sts::123456789012:assumed-role/analysis-reader/x",
    },
}
#: The two AssumeRole calls, in order: without the external id, then with it.
PROBE = {
    "RoleArn": ROLE_ARN,
    "RoleSessionName": "controlvar-warehouse",
    "DurationSeconds": 900,
}
REAL = {**PROBE, "ExternalId": EXTERNAL_ID}


class _Recording:
    """Records each operation's parameters, then lets the real client run it."""

    def __init__(self, client, calls: list) -> None:
        self._client = client
        self._calls = calls

    def __getattr__(self, name):
        def operation(**params):
            self._calls.append((name, params))
            return getattr(self._client, name)(**params)

        return operation


class _StsScript:
    """Every client make_client builds answers the next step of the script.

    A step is ``("answer", expected_params, response)``, ``("refuse",
    expected_params, code, status)`` -- both on the real client under a
    Stubber, which refuses any other parameters -- or ``("raise", exc)``.  A
    client built past the end of the script fails the test.
    """

    def __init__(self, monkeypatch, steps) -> None:
        from botocore.stub import Stubber

        self.steps = list(steps)
        self.calls: list = []
        self.guards: list = []
        self.stubbers: list = []
        real_make = make_client_module.make_client
        real_guard = make_client_module.BeforeSendGuard
        script = self

        class RecordedGuard(real_guard):
            def __init__(self, host, **kwargs):
                super().__init__(host, **kwargs)
                script.guards.append((host, self.warehouse))

        def build(session, service, region, **kwargs):
            client = real_make(session, service, region, **kwargs)
            assert self.steps, "an AWS call the script does not expect"
            step = self.steps.pop(0)
            if step[0] == "raise":
                return _Recording(_Raising(step[1]), self.calls)
            stubber = Stubber(client)
            if step[0] == "answer":
                stubber.add_response("assume_role", step[2], expected_params=step[1])
            else:
                stubber.add_client_error(
                    "assume_role",
                    service_error_code=step[2],
                    service_message=f"refused: {TEMP_SENTINEL}-MESSAGE",
                    http_status_code=step[3],
                    expected_params=step[1],
                )
            stubber.activate()
            self.stubbers.append(stubber)
            return _Recording(client, self.calls)

        monkeypatch.setattr(make_client_module, "BeforeSendGuard", RecordedGuard)
        monkeypatch.setattr(make_client_module, "make_client", build)

    def assert_played(self) -> None:
        assert self.steps == []
        for stubber in self.stubbers:
            stubber.assert_no_pending_responses()


@pytest.fixture
def no_direct_clients(monkeypatch):
    """boto3's module-level shortcuts refuse: every client comes from make_client."""

    def refuse(*args, **kwargs):
        raise AssertionError("boto3.client/resource called directly")

    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3, "resource", refuse)


def _assume(warehouse="redshift", role_arn=ROLE_ARN, external_id=EXTERNAL_ID):
    return make_client_module.assume_role_session(
        _session(),
        role_arn,
        external_id,
        "us-east-1",
        warehouse=warehouse,
        deadline=Deadline(30),
        resolver=lambda host, port: [("93.184.216.34", port)],
    )


def _surfaces(err: WarehouseError, caplog) -> str:
    return "\n".join(
        [
            str(err),
            repr(err),
            err.message,
            repr(err.to_body()),
            repr(err.log_fields()),
            caplog.text,
            "\n".join(repr(r.__dict__) for r in caplog.records),
        ]
    )


def test_assume_role_session_checks_the_trust_policy_then_assumes_the_role(
    aws_env, monkeypatch, no_direct_clients
):
    """Two AssumeRole calls, in this order, each on a client make_client built
    (so under the guard): without the external id, which STS must refuse;
    then with it.  The session that comes back holds the role's credentials."""
    _only_loopback_resolves(monkeypatch)
    script = _StsScript(
        monkeypatch,
        [("refuse", PROBE, "AccessDenied", 403), ("answer", REAL, ASSUMED)],
    )
    session = _assume()
    script.assert_played()
    assert script.calls == [("assume_role", PROBE), ("assume_role", REAL)]
    assert script.guards == [("sts.us-east-1.amazonaws.com", "redshift")] * 2
    held = session.get_credentials()
    assert (held.access_key, held.secret_key, held.token) == (
        "ASIA" + TEMP_SENTINEL,
        "secret-" + TEMP_SENTINEL,
        "token-" + TEMP_SENTINEL,
    )
    assert session.region_name == "us-east-1"
    assert TEMP_SENTINEL not in repr(session)


@pytest.mark.regression
def test_a_role_that_admits_the_deployment_without_the_external_id_is_refused(
    aws_env, monkeypatch, caplog
):
    """#826: a role whose trust policy does not require the external id is
    refused, and the credentials STS gave for it are dropped unused."""
    caplog.set_level(logging.DEBUG)
    _only_loopback_resolves(monkeypatch)
    script = _StsScript(monkeypatch, [("answer", PROBE, ASSUMED)])
    with pytest.raises(WarehouseError) as err:
        _assume()
    script.assert_played()
    assert script.calls == [("assume_role", PROBE)]
    assert err.value.code is WarehouseErrorCode.ROLE_TRUST_UNSAFE
    assert err.value.status_code == 502
    assert err.value.warehouse == "redshift"
    assert err.value.message == (
        "The role's trust policy must require this connection's external ID."
    )
    assert TEMP_SENTINEL not in _surfaces(err.value, caplog)
    assert err.value.__context__ is None


@pytest.mark.parametrize(
    "step,code",
    [
        (("refuse", PROBE, "Throttling", 400), WarehouseErrorCode.WAREHOUSE_BUSY),
        (
            ("refuse", PROBE, "InternalFailure", 500),
            WarehouseErrorCode.UNRECOGNISED_WAREHOUSE_ERROR,
        ),
        # A 403 that is not AccessDenied does not count as the refusal.
        (
            ("refuse", PROBE, "InvalidClientTokenId", 403),
            WarehouseErrorCode.PERMISSION_DENIED,
        ),
        # Nor does an auth_failed whose AWS code is not AccessDenied.
        (
            ("refuse", PROBE, "SignatureDoesNotMatch", 401),
            WarehouseErrorCode.AUTH_FAILED,
        ),
        (("raise", NoCredentialsError()), WarehouseErrorCode.AWS_IDENTITY_UNAVAILABLE),
        (
            (
                "raise",
                EndpointConnectionError(
                    endpoint_url="https://sts.us-east-1.amazonaws.com/"
                ),
            ),
            WarehouseErrorCode.UNREACHABLE,
        ),
    ],
    ids=[
        "throttled",
        "server-error",
        "other-403",
        "other-401",
        "no-identity",
        "unreachable",
    ],
)
def test_only_an_access_denied_check_lets_the_operation_go_on(
    step, code, aws_env, monkeypatch, caplog
):
    """Any other outcome of the first call is the operation's failure, and the
    role is never assumed without the check having been refused."""
    caplog.set_level(logging.DEBUG)
    _only_loopback_resolves(monkeypatch)
    script = _StsScript(monkeypatch, [step])
    with pytest.raises(WarehouseError) as err:
        _assume()
    script.assert_played()
    assert script.calls == [("assume_role", PROBE)]
    assert err.value.code is code
    assert err.value.warehouse == "redshift"
    assert TEMP_SENTINEL not in _surfaces(err.value, caplog)


@pytest.mark.parametrize(
    "refusal,code",
    [
        (("AccessDenied", 403), WarehouseErrorCode.AUTH_FAILED),
        (("Throttling", 400), WarehouseErrorCode.WAREHOUSE_BUSY),
    ],
    ids=["access-denied", "throttled"],
)
def test_sts_refusing_the_role_is_mapped_by_assume_role_sessions_table(
    refusal, code, aws_env, monkeypatch
):
    """STS AccessDenied is auth_failed here, and only here: call() without a
    table still says permission_denied (test_boto_client_errors_are_coded)."""
    _only_loopback_resolves(monkeypatch)
    script = _StsScript(
        monkeypatch,
        [("refuse", PROBE, "AccessDenied", 403), ("refuse", REAL, *refusal)],
    )
    with pytest.raises(WarehouseError) as err:
        _assume()
    script.assert_played()
    assert err.value.code is code
    assert err.value.vendor_code == refusal[0]
    assert err.value.warehouse == "redshift"
    assert "Athena" not in err.value.message


@pytest.mark.parametrize(
    "role_arn,external_id",
    [
        (ROLE_ARN, None),
        (ROLE_ARN, ""),
        (ROLE_ARN, "exp-" + "0" * 31),
        (ROLE_ARN, "exp-" + "A" * 32),
        (ROLE_ARN, EXTERNAL_ID + "\n"),
        (None, EXTERNAL_ID),
        ("arn:aws:iam::12345:role/x", EXTERNAL_ID),
        ("arn:aws:s3:::bucket", EXTERNAL_ID),
        (ROLE_ARN + "\n", EXTERNAL_ID),
    ],
    ids=[
        "no-external-id",
        "empty-external-id",
        "short-external-id",
        "upper-case-external-id",
        "external-id-newline",
        "no-role",
        "short-account",
        "not-a-role",
        "role-newline",
    ],
)
def test_a_bad_role_or_external_id_is_refused_before_any_call(
    role_arn, external_id, aws_env, monkeypatch
):
    script = _StsScript(monkeypatch, [])
    with pytest.raises(WarehouseError) as err:
        _assume(role_arn=role_arn, external_id=external_id)
    assert err.value.code is WarehouseErrorCode.INTERNAL
    assert script.calls == [] and script.guards == []


def test_assume_role_session_requires_an_aws_warehouse(aws_env, monkeypatch):
    _StsScript(monkeypatch, [])
    with pytest.raises(TypeError, match="warehouse"):
        make_client_module.assume_role_session(
            _session(), ROLE_ARN, EXTERNAL_ID, "us-east-1", deadline=Deadline(30)
        )
    with pytest.raises(ValueError):
        _assume(warehouse="bigquery")


# The same calls on the wire: botocore serialises each request, the guard
# sees it, and botocore's parser reads an STS answer.  So whatever botocore
# itself logs -- the request it sends, what it signs, the body it parses --
# is in caplog.


class _RawBody:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def stream(self, **kwargs):
        yield self._body


_STS_NS = 'xmlns="https://sts.amazonaws.com/doc/2011-06-15/"'


def _sts_denied() -> bytes:
    return (
        f"<ErrorResponse {_STS_NS}><Error><Type>Sender</Type>"
        f"<Code>AccessDenied</Code><Message>not for you</Message></Error>"
        f"<RequestId>req-denied</RequestId></ErrorResponse>"
    ).encode()


def _sts_assumed() -> bytes:
    return (
        f"<AssumeRoleResponse {_STS_NS}><AssumeRoleResult><Credentials>"
        f"<AccessKeyId>ASIA{TEMP_SENTINEL}</AccessKeyId>"
        f"<SecretAccessKey>secret-{TEMP_SENTINEL}</SecretAccessKey>"
        f"<SessionToken>token-{TEMP_SENTINEL}</SessionToken>"
        f"<Expiration>2026-11-07T12:15:00Z</Expiration></Credentials>"
        f"<AssumedRoleUser><AssumedRoleId>AROA0123456789ABCDEFG:x</AssumedRoleId>"
        f"<Arn>arn:aws:sts::123456789012:assumed-role/r/x</Arn></AssumedRoleUser>"
        f"</AssumeRoleResult><ResponseMetadata><RequestId>req-ok</RequestId>"
        f"</ResponseMetadata></AssumeRoleResponse>"
    ).encode()


def _sts_identity() -> bytes:
    return (
        f"<GetCallerIdentityResponse {_STS_NS}><GetCallerIdentityResult>"
        f"<Arn>arn:aws:sts::123456789012:assumed-role/r/x</Arn>"
        f"<UserId>AROA0123456789ABCDEFG:x</UserId><Account>123456789012</Account>"
        f"</GetCallerIdentityResult><ResponseMetadata><RequestId>req-id</RequestId>"
        f"</ResponseMetadata></GetCallerIdentityResponse>"
    ).encode()


def _assume_and_call_at_debug(monkeypatch, caplog):
    """The role assumed, then one call signed with its session, with the root
    at DEBUG and the AWS SDK's loggers at the levels core holds them at
    (``quiet_http_client_loggers``, which ``configure_logging`` applies when the
    API starts).  Returns the session and the text of every record."""
    from botocore.awsrequest import AWSResponse

    from backend.app.core.logger import quiet_http_client_loggers

    caplog.set_level(logging.DEBUG)
    quiet_http_client_loggers()
    _only_loopback_resolves(monkeypatch)
    answers = [(403, _sts_denied()), (200, _sts_assumed()), (200, _sts_identity())]
    sent = []
    guarded = []
    real_make = make_client_module.make_client
    real_guard_call = make_client_module.BeforeSendGuard.__call__

    def guard_call(self, request, **kwargs):
        guarded.append(urlsplit(request.url).hostname)
        return real_guard_call(self, request, **kwargs)

    def answer(request, **kwargs):
        body = request.body
        sent.append(
            (
                parse_qs(body.decode() if isinstance(body, bytes) else body),
                dict(request.headers),
            )
        )
        status, payload = answers.pop(0)
        return AWSResponse(
            request.url, status, {"Content-Type": "text/xml"}, _RawBody(payload)
        )

    def build(*args, **kwargs):
        client = real_make(*args, **kwargs)
        client.meta.events.register("before-send", answer)
        return client

    monkeypatch.setattr(make_client_module.BeforeSendGuard, "__call__", guard_call)
    monkeypatch.setattr(make_client_module, "make_client", build)

    session = _assume()
    identity = aws_call(
        session,
        "sts",
        "us-east-1",
        "get_caller_identity",
        warehouse="redshift",
        deadline=Deadline(30),
        resolver=lambda host, port: [("93.184.216.34", port)],
    )

    assert answers == []
    assert guarded == ["sts.us-east-1.amazonaws.com"] * 3
    probe, real, signed = sent
    assert probe[0]["Action"] == ["AssumeRole"] and "ExternalId" not in probe[0]
    assert real[0]["ExternalId"] == [EXTERNAL_ID]
    assert real[0]["RoleSessionName"] == ["controlvar-warehouse"]
    assert real[0]["DurationSeconds"] == ["900"]
    # Positive control: the session signs with the role's credentials.
    assert signed[1]["X-Amz-Security-Token"] in (
        "token-" + TEMP_SENTINEL,
        ("token-" + TEMP_SENTINEL).encode(),
    )
    assert identity["Account"] == "123456789012"
    return session, caplog.text + "\n".join(repr(r.__dict__) for r in caplog.records)


def test_the_temporary_credentials_reach_no_log_and_no_repr(
    aws_env, monkeypatch, caplog
):
    """At DEBUG, through botocore's own serialiser, signer and parser, with the
    AWS SDK's loggers where core holds them: the credentials STS hands out
    appear in no log record and no repr -- not when the answer is parsed, and
    not when the session they make signs a call.  The control below shows the
    same steps would find them."""
    session, logged = _assume_and_call_at_debug(monkeypatch, caplog)
    below_warning = sorted(
        {
            r.name
            for r in caplog.records
            if r.name.split(".")[0] == "botocore" and r.levelno < logging.WARNING
        }
    )
    assert below_warning == []
    leaks = sorted(
        {r.name for r in caplog.records if TEMP_SENTINEL in repr(r.__dict__)}
    )
    assert TEMP_SENTINEL not in logged, leaks
    assert TEMP_SENTINEL not in repr(session)


def test_off_cores_logger_list_the_same_steps_find_botocores_debug_records(
    aws_env, monkeypatch, caplog
):
    """The control for the test above: with ``botocore`` taken off core's
    ``HTTP_CLIENT_LOGGERS``, the same steps put botocore's DEBUG records on the
    handler, and what the test above looks for is in them.  So its absence
    there is measured, not vacuous."""
    from backend.app.core import logger as logger_module

    monkeypatch.setattr(
        logger_module,
        "HTTP_CLIENT_LOGGERS",
        tuple(n for n in logger_module.HTTP_CLIENT_LOGGERS if n != "botocore"),
    )
    # Importing the application already held it at WARNING; restore_root_logger
    # puts that level back after the test.
    logging.getLogger("botocore").setLevel(logging.NOTSET)
    _, logged = _assume_and_call_at_debug(monkeypatch, caplog)
    assert any(
        r.name.startswith("botocore") and r.levelno == logging.DEBUG
        for r in caplog.records
    )
    assert TEMP_SENTINEL in logged


def test_region_list_is_the_committed_data():
    assert len(AWS_REGIONS) == 34
    assert (
        aws_endpoint_host("athena", "eu-west-1", warehouse="athena")
        == "athena.eu-west-1.amazonaws.com"
    )
