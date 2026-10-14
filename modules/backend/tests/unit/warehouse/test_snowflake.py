"""The Snowflake connector, against recorded responses on a fake network.

Every test drives the real client (destination check, address check,
deadline, body limit) over the network fake; nothing reaches Snowflake.
Keys are generated at test time; none is committed.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
import traceback
from datetime import datetime, timezone

import httpcore
import jwt
import pytest
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

from modules.backend.app.core.warehouse_identifiers import WarehouseQueryRefused
from modules.backend.app.models.warehouse_connection import WarehouseConnection
from modules.backend.app.services.warehouse_query_builder import (
    SNOWFLAKE_SQL,
    AnalysisWindow,
    AssignmentMapping,
    BuiltQuery,
    MetricMapping,
    build_diagnostics_query,
    build_metric_query,
)
from modules.backend.app.services.warehouse_sufficient_stats import (
    parse_diagnostics_rows,
    parse_float,
    parse_metric_rows,
)
from modules.backend.app.warehouse import egress
from modules.backend.app.warehouse import snowflake as sf
from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.app.warehouse.egress import GuardedTransport, OutboundClient
from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode
from modules.backend.app.warehouse.snowflake import (
    ADMIN_ROLES,
    CANCEL_RESERVE_SECONDS,
    SESSION_PARAMETERS,
    SnowflakeAdapter,
    SnowflakeConfigRefused,
    SnowflakeConnection,
    SnowflakeKey,
    generate_key_pair,
    set_public_key_statement,
)
from modules.backend.tests.unit.warehouse.conftest import FakeClock
from modules.backend.tests.unit.warehouse.snowflake_fake import (
    HANDLE,
    SENTINEL,
    NetworkFake,
    Script,
    client_factory,
    recorded,
)

pytestmark = pytest.mark.unit

C = WarehouseErrorCode
HOST = "acme-analytics.snowflakecomputing.com"
STATEMENT = f"/api/v2/statements/{HANDLE}"
NOW = 1_759_000_000


def connection(**changes) -> SnowflakeConnection:
    params = {
        "account": "ACME-ANALYTICS",
        "user": "analysis_reader",
        "role": "ANALYSIS_READER_ROLE",
        "warehouse": "ANALYSIS_WH",
        "query_timeout_seconds": 300,
    }
    params.update(changes)
    return SnowflakeConnection(**params)


# -- keys -------------------------------------------------------------------


@pytest.fixture(scope="module")
def pair():
    return generate_key_pair("analysis_reader")


@pytest.fixture(scope="module")
def key(pair) -> SnowflakeKey:
    return SnowflakeKey.from_blob(pair.private_blob)


def adapter(key, fake, **kwargs) -> SnowflakeAdapter:
    kwargs.setdefault("wall_clock", lambda: float(NOW))
    kwargs.setdefault("sleeper", lambda seconds: None)
    return SnowflakeAdapter(
        kwargs.pop("connection", connection()),
        key,
        client_factory=client_factory(fake),
        **kwargs,
    )


ASSIGNMENT = AssignmentMapping(
    table=("ANALYTICS", "PUBLIC", "EXPOSURES"),
    unit_id="USER_ID",
    experiment_key="EXPERIMENT_KEY",
    variant="VARIANT",
    exposed_at="EXPOSED_AT",
)
WINDOW = AnalysisWindow(
    start=datetime(2026, 9, 1, tzinfo=timezone.utc),
    end=datetime(2026, 9, 15, tzinfo=timezone.utc),
)


def metric_query() -> BuiltQuery:
    return build_metric_query(
        SNOWFLAKE_SQL,
        ASSIGNMENT,
        MetricMapping(
            table=("ANALYTICS", "PUBLIC", "ORDERS"),
            unit_id="USER_ID",
            event_at="EVENT_AT",
            metric_type="proportion",
        ),
        "checkout-test",
        WINDOW,
    )


HAPPY = {
    "submit": ["statement_submitted"],
    "status": ["statement_running", "result_metric"],
    "partition": ["partition_1"],
    "cancel": ["cancel_ok"],
}


def happy(**changes) -> Script:
    routes = {k: list(v) for k, v in HAPPY.items()}
    routes.update(changes)
    return Script(routes)


def of_kind(fake, kind):
    return [r for r in fake.requests if Script.key(r) == kind]


def _formatted(exc: BaseException) -> str:
    return "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))


# -- the connection ---------------------------------------------------------


@pytest.mark.parametrize(
    "account",
    [
        "ACME-ANALYTICS\n",
        "ACME-ANALYTICS\r",
        "\nACME-ANALYTICS",
        "xy12345.us-east-1",
        "xy12345",
        "acme-analytics.privatelink",
        "https://acme-analytics.snowflakecomputing.com",
        "acme-analytics.snowflakecomputing.com",
        "acme-analytics:443",
        "acme-analytics/x",
        "acme-analytics@127.0.0.1",
        "1acme-analytics",
        "acme analytics",
        "",
    ],
)
def test_account_must_be_the_organisation_account_identifier(account):
    with pytest.raises(SnowflakeConfigRefused) as err:
        connection(account=account)
    assert err.value.code == "invalid_account"
    assert err.value.field == "account"
    assert "Snowsight > Admin > Accounts" in err.value.message
    assert account.strip() not in err.value.message or not account.strip()


@pytest.mark.parametrize("field", ["user", "role", "warehouse"])
@pytest.mark.parametrize(
    "value",
    [
        "NAME\n",
        "NAME\r",
        "\nNAME",
        'NA"ME',
        "NA'ME",
        "NA ME",
        "NA.ME",
        "1NAME",
        "",
        "N;",
    ],
)
def test_object_names_fully_match(field, value):
    with pytest.raises(SnowflakeConfigRefused) as err:
        connection(**{field: value})
    assert err.value.code == f"invalid_{field}"
    assert err.value.to_body()["field"] == field


@pytest.mark.parametrize(
    "role",
    sorted(ADMIN_ROLES)
    + ["accountadmin", "AccountAdmin", "sysadmin", "securityAdmin", "orgadmin"],
)
def test_administrative_roles_refused_whatever_their_case(role):
    with pytest.raises(SnowflakeConfigRefused) as err:
        connection(role=role)
    assert err.value.code == "role_not_allowed"


def test_the_host_comes_from_the_account_alone():
    assert connection(account="MyOrg-My_Account1").host == (
        "myorg-my-account1.snowflakecomputing.com"
    )
    assert connection(account="MyOrg-My_Account1").jwt_account == "MYORG-MY_ACCOUNT1"


@pytest.mark.parametrize("value", [0, -1, True, 1.5, "300", None])
def test_query_limit_is_a_positive_integer(value):
    with pytest.raises(ValueError):
        connection(query_timeout_seconds=value)


# -- the key pair -----------------------------------------------------------


def test_only_the_public_key_and_statement_are_shown(pair):
    """What a customer is shown carries no private key material in any form."""
    shown = pair.shown.to_body()
    assert set(shown) == {"public_key", "public_key_fingerprint", "statement"}
    private_b64 = base64.b64encode(pair.private_blob).decode()
    for surface in (json.dumps(shown), repr(pair), repr(pair.shown), str(pair)):
        assert private_b64 not in surface
        assert private_b64[40:80] not in surface
        assert "PRIVATE" not in surface
    public = serialization.load_der_public_key(base64.b64decode(shown["public_key"]))
    assert isinstance(public, rsa.RSAPublicKey) and public.key_size == 2048
    assert shown["statement"] == (
        f"ALTER USER analysis_reader SET RSA_PUBLIC_KEY='{shown['public_key']}';"
    )


def test_the_fingerprint_is_the_one_snowflake_computes(pair):
    """SHA256: + base64(sha256(DER SubjectPublicKeyInfo)) -- what DESC USER shows.

    Computed here from the private key, independently of the connector.
    """
    private = serialization.load_der_private_key(pair.private_blob, password=None)
    der = private.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    assert pair.shown.public_key == base64.b64encode(der).decode()
    expected = "SHA256:" + base64.b64encode(hashlib.sha256(der).digest()).decode()
    assert pair.shown.fingerprint == expected
    assert SnowflakeKey.from_blob(pair.private_blob).fingerprint == expected


def test_a_replacement_key_goes_in_the_second_slot():
    replacement = generate_key_pair("analysis_reader", slot=2)
    assert replacement.shown.statement.startswith(
        "ALTER USER analysis_reader SET RSA_PUBLIC_KEY_2='"
    )


def test_every_generated_pair_is_new():
    assert (
        generate_key_pair("u1").shown.fingerprint
        != generate_key_pair("u1").shown.fingerprint
    )


@pytest.mark.parametrize(
    "user, public_key, slot",
    [
        ("analysis_reader\n", "QUJD", 1),
        ("a b", "QUJD", 1),
        ("analysis_reader", "QUJD';DROP", 1),
        ("analysis_reader", "", 1),
        ("analysis_reader", "QUJD", 3),
        ("analysis_reader", "QUJD", True),
    ],
)
def test_the_statement_is_built_only_from_checked_values(user, public_key, slot):
    with pytest.raises(ValueError):
        set_public_key_statement(user, public_key, slot=slot)


def test_the_stored_private_key_round_trips_encrypted(pair):
    keys = Fernet.generate_key().decode()
    row = WarehouseConnection(warehouse_type="snowflake")
    row.set_credentials(pair.private_blob, keys=keys)
    assert pair.private_blob not in bytes(row.credentials_ciphertext)
    restored = SnowflakeKey.from_blob(row.get_credentials(keys=keys))
    assert restored.fingerprint == pair.shown.fingerprint


def _der(private) -> bytes:
    return private.private_bytes(
        serialization.Encoding.DER,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )


@pytest.mark.parametrize(
    "blob",
    [
        pytest.param(b"", id="empty"),
        pytest.param(SENTINEL.encode(), id="garbage"),
        pytest.param(_der(ec.generate_private_key(ec.SECP256R1())), id="not-rsa"),
        pytest.param(
            _der(rsa.generate_private_key(public_exponent=65537, key_size=1024)),
            id="rsa-1024",
        ),
    ],
)
def test_a_stored_blob_that_is_not_a_usable_key_is_internal(blob):
    with pytest.raises(WarehouseError) as err:
        SnowflakeKey.from_blob(blob)
    assert err.value.code is C.INTERNAL
    assert SENTINEL not in _formatted(err.value)


def test_repr_never_shows_the_key(key, pair):
    private_b64 = base64.b64encode(pair.private_blob).decode()
    fake = NetworkFake(happy())
    for text in (repr(key), repr(adapter(key, fake)), repr(pair)):
        assert private_b64[40:80] not in text


# -- sign-in ----------------------------------------------------------------


def test_each_request_carries_the_key_pair_jwt(key, pair):
    """Account and user are upper case in the JWT, however they were entered."""
    fake = NetworkFake(happy())
    adapter(key, fake, connection=connection(account="acme-Analytics")).run_query(
        metric_query(), Deadline(600)
    )
    assert {r.host for r in fake.requests} == {HOST}
    public = serialization.load_der_public_key(base64.b64decode(pair.shown.public_key))
    for request in fake.requests:
        assert request.headers["x-snowflake-authorization-token-type"] == "KEYPAIR_JWT"
        scheme, _, token = request.headers["authorization"].partition(" ")
        assert scheme == "Bearer"
        assert jwt.get_unverified_header(token) == {"alg": "RS256", "typ": "JWT"}
        claims = jwt.decode(
            token,
            public,
            algorithms=["RS256"],
            options={"verify_exp": False, "verify_iat": False},
        )
        assert claims == {
            "iss": f"ACME-ANALYTICS.ANALYSIS_READER.{pair.shown.fingerprint}",
            "sub": "ACME-ANALYTICS.ANALYSIS_READER",
            "iat": NOW,
            "exp": NOW + 3000,
        }


def test_the_jwt_is_signed_again_before_it_ages(key):
    now = [float(NOW)]
    fake = NetworkFake(happy(status=["result_metric"]))
    connector = adapter(key, fake, wall_clock=lambda: now[0])

    def tokens():
        return {r.headers["authorization"] for r in fake.requests}

    connector.run_query(metric_query(), Deadline(600))
    now[0] += sf.JWT_REUSE_SECONDS - 1
    connector.run_query(metric_query(), Deadline(600))
    assert len(tokens()) == 1
    now[0] += 1
    connector.run_query(metric_query(), Deadline(600))
    assert len(tokens()) == 2
    assert sf.JWT_LIFETIME_SECONDS <= 3600


# -- statements -------------------------------------------------------------


def test_snowflake_request_sets_timezone_utc(key):
    """Every statement is sent with timezone UTC and one statement per request."""
    fake = NetworkFake(happy())
    query = metric_query()
    adapter(key, fake).run_query(query, Deadline(600))
    (submit,) = of_kind(fake, "submit")
    assert (submit.method, submit.host, submit.path) == (
        "POST",
        HOST,
        "/api/v2/statements",
    )
    assert submit.query["async"] == ["true"]
    assert len(submit.query["requestId"]) == 1
    body = submit.json()
    assert (
        body["parameters"]
        == SESSION_PARAMETERS
        == {
            "multi_statement_count": "1",
            "timezone": "UTC",
            "query_tag": "experimently-analysis",
        }
    )
    assert body == {
        "statement": query.sql,
        "timeout": 300,
        "warehouse": "ANALYSIS_WH",
        "role": "ANALYSIS_READER_ROLE",
        "database": "ANALYTICS",
        "parameters": SESSION_PARAMETERS,
    }


def test_the_server_side_limit_never_exceeds_the_time_left_and_is_never_zero(key):
    clock = FakeClock()
    deadline = Deadline(10, clock=clock)
    clock.advance(9.6)
    fake = NetworkFake(happy(status=["result_metric"]))
    with pytest.raises(WarehouseError):
        adapter(key, fake).run_query(metric_query(), deadline)
    assert of_kind(fake, "submit")[0].json()["timeout"] == 1

    fake = NetworkFake(happy())
    adapter(key, fake).run_query(metric_query(), Deadline(42))
    assert of_kind(fake, "submit")[0].json()["timeout"] in (41, 42)


def test_run_query_polls_and_returns_the_rows(key):
    clock = FakeClock()
    fake = NetworkFake(happy(), clock=clock, seconds_per_request=0.4)
    result = adapter(key, fake, monotonic=clock).run_query(
        metric_query(), Deadline(600, clock=clock)
    )
    statuses = of_kind(fake, "status")
    assert len(statuses) == 2
    for poll in statuses:
        assert (poll.method, poll.host, poll.path) == ("GET", HOST, STATEMENT)
    assert not of_kind(fake, "cancel")
    assert result.statement_handle == HANDLE
    assert result.warehouse == "ANALYSIS_WH"
    # three requests at 0.4 fake seconds each (the pause sleeper is a no-op)
    assert result.elapsed_ms == 1200
    # Snowflake's upper-case alias names are read in lower case.
    assert set(result.rows[0]) == {
        "variant",
        "n",
        "n_converted",
        "k",
        "sum_d",
        "sum_d2",
        "metric_rows_in_window",
        "metric_rows_matched",
        "null_value_rows",
        "session_offset",
    }
    stats = parse_metric_rows(list(result.rows), expect_session_offset=True)
    assert [v.variant for v in stats.variants] == ["treatment", "control"]
    assert stats.k_text == "0.1075"
    assert {r.host for r in fake.requests} == {HOST}
    assert set(fake.tls_hostnames) == {HOST}
    assert {c[0] for c in fake.connects} == {"93.184.216.34"}


def test_diagnostics_statement_reads_back(key):
    fake = NetworkFake(happy(status=["result_diagnostics"]))
    query = build_diagnostics_query(SNOWFLAKE_SQL, ASSIGNMENT, "checkout-test", WINDOW)
    result = adapter(key, fake).run_query(query, Deadline(600))
    diagnostics = parse_diagnostics_rows(list(result.rows), expect_session_offset=True)
    assert diagnostics.units == 2010


@pytest.mark.parametrize(
    "answer",
    [
        "result_metric_offset_not_utc",
        (lambda s, b: (s, {**b, "data": [r[:9] + [None] for r in b["data"]]}))(
            *recorded("result_metric")
        ),
        (lambda s, b: (s, {**b, "data": [r[:9] + ["+00:00 "] for r in b["data"]]}))(
            *recorded("result_metric")
        ),
    ],
    ids=["minus-seven", "missing", "padded"],
)
def test_run_fails_when_session_offset_not_utc(key, answer):
    """A session that did not run in UTC is refused, never analysed."""
    fake = NetworkFake(happy(status=[answer]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.TIMEZONE_NOT_UTC


def test_every_partition_is_read(key):
    fake = NetworkFake(happy(status=["result_two_partitions"]))
    result = adapter(key, fake).run_query(metric_query(), Deadline(600))
    (part,) = of_kind(fake, "partition")
    assert (part.method, part.host, part.path) == ("GET", HOST, STATEMENT)
    assert part.query["partition"] == ["1"]
    assert [row["variant"] for row in result.rows] == ["treatment", "control"]


def test_more_partitions_than_a_result_can_need_refused(key):
    status, body = recorded("result_two_partitions")
    body["resultSetMetaData"]["partitionInfo"] = [{"rowCount": 1}] * 6
    fake = NetworkFake(happy(status=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.RESULT_INVALID
    assert not of_kind(fake, "partition")


class _ProbeOrSnowflake(httpcore.NetworkBackend):
    """Loopback goes, for real, to the probe; every other address to the fake."""

    def __init__(self, probe, fake) -> None:
        self._probe = probe
        self._fake = fake
        self._real = httpcore.SyncBackend()

    def connect_tcp(
        self, host, port, timeout=None, local_address=None, socket_options=None
    ):
        if host == "127.0.0.1":
            return self._real.connect_tcp(host, self._probe.port, timeout=1.0)
        return self._fake.connect_tcp(host, port)

    def connect_unix_socket(self, path, timeout=None, socket_options=None):
        raise httpcore.ConnectError("no")

    def sleep(self, seconds):
        pass


def test_status_url_from_snowflake_is_never_followed_and_probe_receives_nothing(
    key, probe, monkeypatch
):
    """The poll URL is built from the account's host and the handle, never taken
    from the answer.

    The layers in front are opened so the probe *can* be reached: the
    destination check allows any URL and the address check allows loopback.
    Snowflake's answers then name the probe as the statement's status URL,
    and the connector must still poll the account's own host.
    """
    from modules.backend.tests.unit.warehouse.test_egress import _wait_for_probe

    original = egress.refused_address
    monkeypatch.setattr(
        egress,
        "refused_address",
        lambda value: value != "127.0.0.1" and original(value),
    )
    probe_url = f"http://127.0.0.1:{probe.port}/api/v2/statements/{HANDLE}"
    steered = []
    for name in ("statement_submitted", "statement_running", "result_metric"):
        status, body = recorded(name)
        body["statementStatusUrl"] = probe_url
        steered.append((status, body))
    fake = NetworkFake(
        Script(
            {
                "submit": [steered[0]],
                "status": [steered[1], steered[2]],
                "cancel": ["cancel_ok"],
            }
        )
    )

    def resolve(host, port):
        if host == "127.0.0.1":
            return [("127.0.0.1", probe.port)]
        return [("93.184.216.34", port)]

    def make(deadline):
        return OutboundClient(
            deadline,
            warehouse="snowflake",
            transport=GuardedTransport(
                deadline,
                resolver=resolve,
                network_backend=_ProbeOrSnowflake(probe, fake),
                destination_check=lambda url: None,
            ),
        )

    connector = SnowflakeAdapter(
        connection(), key, client_factory=make, sleeper=lambda s: None
    )
    outcome = None
    try:
        connector.run_query(metric_query(), Deadline(600))
    except WarehouseError as exc:
        outcome = exc.code
    _wait_for_probe(probe)
    assert probe.accepted == 0
    assert [(r.host, r.path) for r in of_kind(fake, "status")] == [
        (HOST, STATEMENT),
        (HOST, STATEMENT),
    ]
    assert outcome is None


@pytest.mark.parametrize(
    "handle",
    [
        "../../api/v2/other",
        f"{HANDLE}?partition=0",
        f"{HANDLE}/cancel",
        f"{HANDLE}\n",
        "http://127.0.0.1:1/x",
        "019c06a4-0000-df4f-0000",
        "",
        None,
        12,
    ],
)
def test_a_handle_that_is_not_a_uuid_is_refused_before_it_is_used(key, handle):
    status, body = recorded("statement_submitted")
    body["statementHandle"] = handle
    fake = NetworkFake(happy(submit=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.RESULT_INVALID
    assert [Script.key(r) for r in fake.requests] == ["submit"]


def test_an_answer_about_another_statement_is_refused(key):
    status, body = recorded("result_metric")
    body["statementHandle"] = "019c06a4-0000-df4f-0000-00100006ffff"
    fake = NetworkFake(happy(status=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.RESULT_INVALID


# -- deadlines and cancel ---------------------------------------------------


def test_cancel_on_deadline(key):
    """A statement still running at the deadline is cancelled, and the query is time_limit."""
    clock = FakeClock()
    fake = NetworkFake(
        happy(status=["statement_running"]), clock=clock, seconds_per_request=3.0
    )
    connector = adapter(key, fake, sleeper=lambda s: clock.advance(s))
    end = clock() + 60
    with pytest.raises(WarehouseError) as err:
        connector.run_query(metric_query(), Deadline(60, clock=clock))
    assert err.value.code is C.TIME_LIMIT
    (cancel,) = of_kind(fake, "cancel")
    assert (cancel.method, cancel.host, cancel.path) == (
        "POST",
        HOST,
        STATEMENT + "/cancel",
    )
    # Polls stop early enough for the cancel to go out within the total.
    assert cancel.at is not None and cancel.at <= end
    assert len(of_kind(fake, "status")) > 3


def test_cancel_is_sent_even_when_a_slow_answer_used_the_reserve(key):
    clock = FakeClock()
    script = happy(status=["statement_running"])

    def handler(request):
        if Script.key(request) == "status":
            clock.advance(100.0)  # the answer arrives long after the deadline
        return script(request)

    fake = NetworkFake(handler, clock=clock)
    connector = adapter(key, fake, sleeper=lambda s: clock.advance(s))
    with pytest.raises(WarehouseError) as err:
        connector.run_query(metric_query(), Deadline(60, clock=clock))
    assert err.value.code is C.TIME_LIMIT
    assert len(of_kind(fake, "cancel")) == 1


def test_an_unreadable_answer_about_a_known_statement_cancels_it(key):
    fake = NetworkFake(
        happy(status=[(200, b"<html>" + SENTINEL.encode() + b"</html>")])
    )
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.RESULT_INVALID
    assert [c.path for c in of_kind(fake, "cancel")] == [STATEMENT + "/cancel"]


def test_a_lost_submit_answer_is_bounded_by_the_server_side_limit(key):
    """With no handle there is nothing to cancel: the body's timeout stops it."""
    fake = NetworkFake(happy(submit=[(202, b"not json")]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.RESULT_INVALID
    assert not of_kind(fake, "cancel")
    assert of_kind(fake, "submit")[0].json()["timeout"] == 300


def test_a_refused_statement_is_not_cancelled(key):
    fake = NetworkFake(happy(status=["error_insufficient_privileges"]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.PERMISSION_DENIED
    assert not of_kind(fake, "cancel")


def test_a_failed_cancel_still_reports_time_limit(key, caplog):
    clock = FakeClock()
    fake = NetworkFake(
        happy(status=["statement_running"], cancel=["error_insufficient_privileges"]),
        clock=clock,
        seconds_per_request=3.0,
    )
    caplog.set_level(logging.DEBUG)
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake, sleeper=lambda s: clock.advance(s)).run_query(
            metric_query(), Deadline(60, clock=clock)
        )
    assert err.value.code is C.TIME_LIMIT
    assert SENTINEL not in caplog.text


def test_a_rate_limited_poll_is_asked_again(key):
    fake = NetworkFake(
        happy(status=["statement_running", "error_rate_limited", "result_metric"])
    )
    result = adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert len(result.rows) == 2
    assert len(of_kind(fake, "status")) == 3


def test_the_deadline_before_any_request_sends_nothing(key):
    clock = FakeClock()
    deadline = Deadline(1, clock=clock)
    clock.advance(2)
    fake = NetworkFake(happy())
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).check_connection(deadline)
    assert err.value.code is C.TIME_LIMIT
    assert fake.requests == []
    assert CANCEL_RESERVE_SECONDS > 0


# -- errors -----------------------------------------------------------------


@pytest.mark.parametrize(
    "answer, code",
    [
        ("error_invalid_identifier", C.OBJECT_NOT_FOUND),
        ("error_object_not_found", C.OBJECT_NOT_FOUND),
        ("error_insufficient_privileges", C.PERMISSION_DENIED),
        ("error_statement_count", C.NOT_A_SELECT),
        ("error_statement_timeout", C.TIME_LIMIT),
        ("error_jwt_invalid", C.AUTH_FAILED),
        ("error_unknown_code", C.UNRECOGNISED_WAREHOUSE_ERROR),
        (
            (500, b"<html>" + SENTINEL.encode() + b"</html>"),
            C.UNRECOGNISED_WAREHOUSE_ERROR,
        ),
    ],
)
@pytest.mark.parametrize("stage", ["submit", "status"])
def test_snowflake_errors_map_to_codes_without_their_text(
    key, caplog, answer, code, stage
):
    fake = NetworkFake(happy(**{stage: [answer]}))
    caplog.set_level(logging.DEBUG)
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is code
    for surface in (
        str(err.value),
        repr(err.value),
        err.value.message,
        json.dumps(err.value.to_body()),
        json.dumps(err.value.log_fields()),
        _formatted(err.value),
        caplog.text,
        "".join(_formatted(r.exc_info[1]) for r in caplog.records if r.exc_info),
    ):
        assert SENTINEL not in surface
        assert "JWT token is invalid" not in surface
    if code is C.UNRECOGNISED_WAREHOUSE_ERROR and isinstance(answer, str):
        assert err.value.vendor_code == "001003"
        assert "(code 001003)" in err.value.message


@pytest.mark.parametrize(
    "body, status, code",
    [
        ({"code": "003001", "sqlState": "99999"}, 422, C.PERMISSION_DENIED),
        ({"code": "002003", "sqlState": "99999"}, 422, C.OBJECT_NOT_FOUND),
        ({"code": "000630", "sqlState": "99999"}, 422, C.TIME_LIMIT),
        ({"code": "000604"}, 422, C.CANCELLED),
        ({"code": "000008"}, 422, C.NOT_A_SELECT),
        ({"code": "390144"}, 401, C.AUTH_FAILED),
        ({"code": "999999", "sqlState": "42501"}, 422, C.PERMISSION_DENIED),
        ({"code": "999999", "sqlState": "42S02"}, 422, C.OBJECT_NOT_FOUND),
        ({"code": "999999", "sqlState": "57014"}, 422, C.CANCELLED),
        ({"code": "999999", "sqlState": "42000"}, 422, C.UNRECOGNISED_WAREHOUSE_ERROR),
        ({}, 403, C.PERMISSION_DENIED),
        ({"code": ["000630"]}, 408, C.TIME_LIMIT),
    ],
)
def test_error_codes_map_by_code_then_sql_state_then_status(body, status, code):
    error = sf.error_for_body(body, status)
    assert error.code is code
    assert error.warehouse == "snowflake"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda b: b["data"][0].__setitem__(1, 1000),
        lambda b: b["data"][0].pop(),
        lambda b: b["resultSetMetaData"]["rowType"][1].update(name="Variant"),
        lambda b: b["resultSetMetaData"]["rowType"][1].update(name="N-1"),
        lambda b: b["resultSetMetaData"].update(format="arrowv1"),
        lambda b: b["resultSetMetaData"].pop("rowType"),
        lambda b: b.update(data={"0": []}),
        lambda b: b["resultSetMetaData"].update(partitionInfo=[]),
    ],
)
def test_result_of_an_unexpected_shape_refused(key, mutate):
    status, body = recorded("result_metric")
    mutate(body)
    fake = NetworkFake(happy(status=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(metric_query(), Deadline(600))
    assert err.value.code is C.RESULT_INVALID


def test_only_generated_snowflake_statements_run(key):
    fake = NetworkFake(happy())
    other = BuiltQuery(kind="metric", dialect="bigquery", sql="SELECT 1")
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).run_query(other, Deadline(600))
    assert err.value.code is C.INTERNAL
    with pytest.raises(WarehouseError):
        adapter(key, fake).run_query("SELECT 1", Deadline(600))  # type: ignore[arg-type]
    assert fake.requests == []


def test_no_secret_reaches_a_log_record(key, pair, caplog):
    """At DEBUG, with httpx and httpcore tracing, no key or JWT is logged."""
    caplog.set_level(logging.DEBUG)
    # The application holds these two at WARNING (configure_logging); the test
    # turns their tracing back on, which is what it exists to read.
    caplog.set_level(logging.DEBUG, logger="httpx")
    caplog.set_level(logging.DEBUG, logger="httpcore")
    fake = NetworkFake(happy())
    adapter(key, fake).run_query(metric_query(), Deadline(600))
    token = fake.requests[0].headers["authorization"].split(" ", 1)[1]
    private_b64 = base64.b64encode(pair.private_blob).decode()
    text = caplog.text + "".join(str(r.__dict__) for r in caplog.records)
    assert any(r.name.split(".")[0] in ("httpx", "httpcore") for r in caplog.records)
    for secret in (token, token.split(".")[2], private_b64[40:80], "PRIVATE KEY"):
        assert secret not in text


# -- the connection test and metadata ----------------------------------------


def test_connection_test_runs_no_table_query_and_checks_utc(key):
    fake = NetworkFake(happy(status=["result_connection_test"]))
    check = adapter(key, fake).check_connection(Deadline(30))
    assert (check.role, check.warehouse, check.session_offset) == (
        "ANALYSIS_READER_ROLE",
        "ANALYSIS_WH",
        "+00:00",
    )
    (submit,) = of_kind(fake, "submit")
    body = submit.json()
    assert body["statement"] == sf.CONNECTION_TEST_SQL
    assert "database" not in body
    assert body["parameters"]["timezone"] == "UTC"


def test_connection_test_in_another_time_zone_refused(key):
    status, body = recorded("result_connection_test")
    body["data"][0][2] = "-07:00"
    fake = NetworkFake(happy(status=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).check_connection(Deadline(30))
    assert err.value.code is C.TIMEZONE_NOT_UTC


def test_table_columns_from_show_columns(key):
    fake = NetworkFake(happy(status=["real_show_columns"]))
    columns = adapter(key, fake).table_columns(
        ("ANALYTICS", "PUBLIC", "EXPOSURES"), Deadline(30)
    )
    assert columns == (
        ("USER_ID", "TEXT"),
        ("EXPERIMENT_KEY", "TEXT"),
        ("VARIANT", "TEXT"),
        ("EXPOSED_AT", "TIMESTAMP_NTZ"),
    )
    body = of_kind(fake, "submit")[0].json()
    assert body["statement"] == 'SHOW COLUMNS IN TABLE "ANALYTICS"."PUBLIC"."EXPOSURES"'
    assert body["database"] == "ANALYTICS"


@pytest.mark.parametrize(
    "table",
    [
        ("ANALYTICS", "PUBLIC"),
        ("ANALYTICS", "PUBLIC", "EXPOSURES", "X"),
        ("ANALYTICS", 'PUB"LIC', "EXPOSURES"),
        ("ANALYTICS", "PUBLIC", "EXPOSURES\n"),
        ("ANALYTICS", "PUBLIC", "EXPOSURES;"),
        ("ANALYTICS", "PUBLIC", "EXPO SURES"),
        ("ANALYTICS", "PUBLIC", "EXPOSURES--"),
    ],
)
def test_table_reference_checked_before_anything_is_sent(key, table):
    fake = NetworkFake(happy())
    with pytest.raises(WarehouseQueryRefused) as err:
        adapter(key, fake).table_columns(table, Deadline(30))
    assert err.value.code == "invalid_identifier"
    assert fake.requests == [] and fake.connects == []


@pytest.mark.parametrize(
    "data_type",
    ['{"type":"text"}', "TEXT", '{"type":"TEXT; DROP"}', None, "[" * 5000],
    ids=["lower-case", "not-json", "not-a-type-name", "null", "deeply-nested"],
)
def test_a_column_type_of_an_unexpected_shape_refused(key, data_type):
    status, body = recorded("real_show_columns")
    body["data"][0][3] = data_type
    fake = NetworkFake(happy(status=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).table_columns(
            ("ANALYTICS", "PUBLIC", "EXPOSURES"), Deadline(30)
        )
    assert err.value.code is C.RESULT_INVALID


# -- answers recorded on a real account (run wl-snowflake-20261004T225624Z-3787b33c)


#: What the live check's statements read back, as BuiltQuery previews.
def _preview(sql: str) -> BuiltQuery:
    return BuiltQuery(kind="preview", dialect="snowflake", sql=sql)


@pytest.mark.regression
def test_real_connection_test_reports_utc_as_z(key):
    """A real UTC session reports its offset as ``Z``; the check accepts it.

    The first real run failed every check with ``timezone_not_utc`` because
    only ``+00:00`` was accepted.
    """
    fake = NetworkFake(happy(status=["real_connection_test"]))
    check = adapter(key, fake).check_connection(Deadline(30))
    assert (check.role, check.warehouse, check.session_offset) == (
        "ANALYSIS_READER_ROLE",
        "ANALYSIS_WH",
        "Z",
    )


@pytest.mark.regression
def test_real_timezone_probe_reads_back_in_utc(key):
    fake = NetworkFake(happy(status=["real_timezone_probe"]))
    result = adapter(key, fake).run_query(_preview("SELECT 1"), Deadline(30))
    assert result.rows == ({"v": "2026-09-01 00:30:00 Z", "session_offset": "Z"},)


@pytest.mark.regression
def test_real_wire_probe_parses_to_exactly_the_binary64_sum(key):
    """Snowflake's DECFLOAT text is a 38-digit exponent form, not the shortest form.

    The product's parser reads it back as exactly ``0.1 + 0.2``.
    """
    fake = NetworkFake(happy(status=["real_wire_probe"]))
    result = adapter(key, fake).run_query(_preview("SELECT 1"), Deadline(30))
    (row,) = result.rows
    assert row["v"] == "3.0000000000000004440892098500626161695e-1"
    value, text = parse_float(row["v"], "v")
    assert value == 0.1 + 0.2 == 0.30000000000000004
    assert text == row["v"]


@pytest.mark.regression
def test_real_show_columns_is_read(key):
    """Real ``SHOW COLUMNS`` output has a column named ``null?``.

    The first real run refused the whole answer as ``result_invalid``, and so
    every check that needs the table's columns errored.
    """
    fake = NetworkFake(happy(status=["real_show_columns"]))
    columns = adapter(key, fake).table_columns(
        ("ANALYTICS", "PUBLIC", "EXPOSURES"), Deadline(30)
    )
    assert columns == (
        ("USER_ID", "TEXT"),
        ("EXPERIMENT_KEY", "TEXT"),
        ("VARIANT", "TEXT"),
        ("EXPOSED_AT", "TIMESTAMP_NTZ"),
    )
    assert not of_kind(fake, "cancel")


def test_real_metric_statement_is_read_and_parsed(key):
    """A proportion metric statement as a real account answered it."""
    fake = NetworkFake(happy(status=["real_metric"]))
    result = adapter(key, fake).run_query(metric_query(), Deadline(600))
    stats = parse_metric_rows(list(result.rows), expect_session_offset=True)
    assert [v.variant for v in stats.variants] == ["control", "treatment"]
    assert [v.n for v in stats.variants] == [200_000, 200_000]
    assert [v.n_converted for v in stats.variants] == [100_514, 100_260]
    assert stats.k_text == "0.50193500000000002"
    assert not of_kind(fake, "cancel")


def test_real_diagnostics_statement_is_read_and_parsed(key):
    fake = NetworkFake(happy(status=["real_diagnostics"]))
    query = build_diagnostics_query(SNOWFLAKE_SQL, ASSIGNMENT, "checkout-test", WINDOW)
    result = adapter(key, fake).run_query(query, Deadline(600))
    diagnostics = parse_diagnostics_rows(list(result.rows), expect_session_offset=True)
    assert diagnostics.units == 400_000
    assert diagnostics.multi_variant_units == 0


@pytest.mark.parametrize(
    "offset",
    [
        "+01:00",
        "-07:00",
        "-00:00",
        "+0000",
        "+00",
        "00:00",
        "UTC",
        "GMT",
        "z",
        "Z ",
        " Z",
        "ZZ",
        "Z+00:00",
        "+00:00 ",
        "",
        None,
    ],
)
def test_real_connection_test_in_any_other_offset_refused(key, offset):
    """Only the exact UTC spellings pass; anything else is ``timezone_not_utc``."""
    status, body = recorded("real_connection_test")
    body["data"][0][2] = offset
    fake = NetworkFake(happy(status=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).check_connection(Deadline(30))
    assert err.value.code is C.TIMEZONE_NOT_UTC


@pytest.mark.parametrize(
    "name",
    ["null??", "?null", "nu?ll", "null?x", "null!", "null ?", "N-1", "", "?"],
)
def test_real_show_columns_with_another_column_name_refused(key, name):
    """A trailing ``?`` is the only addition to the identifier rule."""
    status, body = recorded("real_show_columns")
    body["resultSetMetaData"]["rowType"][4]["name"] = name
    fake = NetworkFake(happy(status=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).table_columns(
            ("ANALYTICS", "PUBLIC", "EXPOSURES"), Deadline(30)
        )
    assert err.value.code is C.RESULT_INVALID


def test_two_result_columns_differing_only_in_case_still_refused(key):
    status, body = recorded("real_show_columns")
    body["resultSetMetaData"]["rowType"][5]["name"] = "NULL?"
    fake = NetworkFake(happy(status=[(status, body)]))
    with pytest.raises(WarehouseError) as err:
        adapter(key, fake).table_columns(
            ("ANALYTICS", "PUBLIC", "EXPOSURES"), Deadline(30)
        )
    assert err.value.code is C.RESULT_INVALID


def test_every_request_is_to_the_account_host(key):
    fake = NetworkFake(happy(status=["result_connection_test"]))
    connector = adapter(key, fake)
    connector.check_connection(Deadline(30))
    assert fake.connects and {c[0] for c in fake.connects} == {"93.184.216.34"}
    assert {r.host for r in fake.requests} == {HOST}
    assert all(r.path.startswith(sf.STATEMENTS_PATH) for r in fake.requests)
