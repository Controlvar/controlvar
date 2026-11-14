"""The Amazon Redshift connector against a scripted Data API.

Every AWS call is made by the real botocore client for its service, under a
Stubber that holds the exact parameters the call must carry and an answer in
the shape the installed model gives (:mod:`.redshift_fake`).  The clock is a
:class:`~.conftest.FakeClock`; nothing waits and nothing is sent.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List

import boto3
import botocore.session
import pytest

from modules.backend.app.core.warehouse_identifiers import WarehouseQueryRefused
from modules.backend.app.services import warehouse_clients as clients
from modules.backend.app.services.warehouse_query_builder import (
    REDSHIFT_SQL,
    BuiltQuery,
)
from modules.backend.app.warehouse import aws, redshift
from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode
from modules.backend.app.warehouse.redshift import (
    REDSHIFT_CODES,
    UNMODELLED_CODES,
    RedshiftAdapter,
    described,
)
from modules.backend.tests.unit.warehouse import redshift_fake as fake
from modules.backend.tests.unit.warehouse.conftest import FakeClock
from modules.backend.tests.unit.warehouse.test_golden_sql import build_case

pytestmark = pytest.mark.unit

_C = WarehouseErrorCode

#: The statement the batch tests run: the diagnostics golden.
DIAGNOSTICS = build_case("redshift", "diagnostics")
DIAGNOSTIC_NAMES = [
    "exposure_rows",
    "null_key_rows",
    "units",
    "multi_variant_units",
    "variant_values",
]
DIAGNOSTIC_ROW = [fake.long(n) for n in (40, 1, 30, 2, 2)]
OPERATIONS = [
    ("sts", "assume_role"),
    ("sts", "assume_role"),
    ("redshift-data", "batch_execute_statement"),
    ("redshift-data", "describe_statement"),
    ("redshift-data", "describe_statement"),
    ("redshift-data", "get_statement_result"),
    ("redshift-data", "get_statement_result"),
]


@pytest.fixture
def aws_env(monkeypatch):
    for name in (
        "AWS_PROFILE",
        "AWS_SESSION_TOKEN",
        "AWS_ENDPOINT_URL",
        "AWS_ENDPOINT_URL_REDSHIFT_DATA",
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


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


def _script(monkeypatch, clock, steps) -> fake.AwsScript:
    return fake.AwsScript(monkeypatch, steps, clock)


def _run(adapter: RedshiftAdapter, clock: FakeClock, built=DIAGNOSTICS, seconds=1000):
    return adapter.run_query(built, fake.deadline(clock, seconds))


# -- the batch (row: the batch is exact) --------------------------------------


@pytest.mark.parametrize(
    ("conn", "target"),
    [(fake.connection, fake.SERVERLESS), (fake.provisioned, fake.PROVISIONED)],
    ids=["serverless", "provisioned"],
)
def test_a_statement_is_one_batch_exactly_as_specified(
    conn, target, aws_env, monkeypatch, clock
):
    """Five statements in this order, one transaction, JSON results, our name;
    the probe read from :4, the result from :5, nothing else called."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + fake.happy_batch(
            DIAGNOSTICS.sql, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW], target=target
        ),
    )
    result = _run(fake.adapter(conn(), clock), clock)
    script.assert_played()
    assert script.operations() == OPERATIONS
    batch = script.calls[2][2]
    assert batch["Sqls"] == [
        "SET timezone TO 'UTC'",
        "SET extra_float_digits TO 2",
        "SET statement_timeout TO 310000",
        fake.PROBE,
        DIAGNOSTICS.sql,
    ]
    assert sorted(batch) == sorted(
        ["Sqls", "ExecutionMode", "ResultFormat", "StatementName", *target]
    )
    for never in (
        "SessionKeepAliveSeconds",
        "SessionId",
        "WaitTimeSeconds",
        "SecretArn",
    ):
        assert never not in batch
    assert [call[2]["Id"] for call in script.calls[5:]] == [
        f"{fake.STATEMENT_ID}:4",
        f"{fake.STATEMENT_ID}:5",
    ]
    assert result.rows == (dict(zip(DIAGNOSTIC_NAMES, (40, 1, 30, 2, 2))),)
    assert result.statement_handle == fake.STATEMENT_ID
    assert result.warehouse == conn().location


def test_the_installed_botocore_models_every_parameter_the_batch_sends():
    """The build-time check: an older botocore (1.40.43 has no ExecutionMode)
    would refuse the batch before sending it, as ``internal``."""
    model = botocore.session.get_session().get_service_model("redshift-data")
    members = model.operation_model("BatchExecuteStatement").input_shape.members
    assert {"ExecutionMode", "ResultFormat", "StatementName", "Sqls"} <= set(members)
    assert set(members["ExecutionMode"].enum) == {"TRANSACTION", "AUTO_COMMIT"}
    assert "JSON" in members["ResultFormat"].enum
    for operation in (
        "DescribeStatement",
        "GetStatementResult",
        "CancelStatement",
        "DescribeTable",
    ):
        assert operation in model.operation_names


@pytest.mark.parametrize(
    "subs",
    [
        fake.sub_statements(("FINISHED",) * 4, has_result=(False, False, True, True)),
        fake.sub_statements(("FINISHED",) * 6, has_result=(False,) * 3 + (True,) * 3),
        list(reversed(fake.sub_statements())),
        fake.sub_statements(statement_id="00000000-0000-0000-0000-000000000000"),
        fake.sub_statements(("FINISHED",) * 4 + ("FAILED",)),
    ],
    ids=["four", "six", "reordered", "another-batch", "one-not-finished"],
)
def test_a_finished_batch_must_report_exactly_its_five_sub_statements(
    subs, aws_env, monkeypatch, clock
):
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.describe_step("FINISHED", subs),
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID
    assert ("redshift-data", "get_statement_result") not in script.operations()


def test_the_probe_is_read_from_four_and_the_result_from_five(
    aws_env, monkeypatch, clock
):
    """Each read names its sub-statement: swapping them reads a probe as the
    result (or the result as the probe) and the Stubber refuses the Id."""
    _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + fake.happy_batch(DIAGNOSTICS.sql, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW]),
    )
    result = _run(fake.adapter(clock=clock), clock)
    assert set(result.rows[0]) == set(DIAGNOSTIC_NAMES)
    assert "probe" not in result.rows[0]


def test_the_server_side_limit_follows_the_connection_and_the_time_left(
    aws_env, monkeypatch, clock
):
    """The connection's 300 s, plus the 10 s margin; with 120 s of the
    operation left, 115 s (5 s kept to cancel in), plus the margin."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + fake.happy_batch(
            DIAGNOSTICS.sql, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW], timeout_ms=125_000
        ),
    )
    _run(fake.adapter(clock=clock), clock, seconds=120)
    script.assert_played()


# -- the probe -------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "zone", "code"),
    [
        ("0.3", "UTC", _C.RESULT_INVALID),
        ("0.300000000000000", "UTC", _C.RESULT_INVALID),
        ("3.0000000000000004e-1", "UTC", None),
        ("not a number", "UTC", _C.RESULT_INVALID),
        ("0.30000000000000004", "America/New_York", _C.TIMEZONE_NOT_UTC),
        ("0.30000000000000004", "utc", _C.TIMEZONE_NOT_UTC),
        ("0.30000000000000004", "UTC ", _C.TIMEZONE_NOT_UTC),
    ],
)
def test_the_probe_must_read_back_exactly(
    text, zone, code, aws_env, monkeypatch, clock
):
    """0.1 + 0.2 must come back as binary64's 0.30000000000000004 (17 digits)
    and the session's zone as UTC; otherwise the result is not read."""
    steps = fake.assume_steps() + [
        fake.batch_step(DIAGNOSTICS.sql),
        fake.describe_step("FINISHED", fake.sub_statements()),
        fake.probe_step(text, zone),
    ]
    if code is None:
        steps.append(fake.result_step(5, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW]))
    script = _script(monkeypatch, clock, steps)
    adapter = fake.adapter(clock=clock)
    if code is None:
        assert _run(adapter, clock).rows
    else:
        with pytest.raises(WarehouseError) as err:
            _run(adapter, clock)
        assert err.value.code is code
    script.assert_played()


@pytest.mark.parametrize(
    "probe",
    [
        fake.result_step(4, ["probe", "tz"], []),
        fake.result_step(
            4,
            ["probe", "tz"],
            [
                [fake.string("0.30000000000000004"), fake.string("UTC")],
                [fake.string("0.30000000000000004"), fake.string("UTC")],
            ],
        ),
        fake.result_step(
            4, ["v", "tz"], [[fake.string("0.30000000000000004"), fake.string("UTC")]]
        ),
        fake.result_step(4, ["probe", "tz"], [[fake.NULL, fake.string("UTC")]]),
    ],
    ids=["no-row", "two-rows", "wrong-name", "null"],
)
def test_a_probe_of_another_shape_is_refused(probe, aws_env, monkeypatch, clock):
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.describe_step("FINISHED", fake.sub_statements()),
            probe,
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID


# -- the lifecycle ------------------------------------------------------------------


def test_a_finished_select_without_a_result_set_is_invalid(aws_env, monkeypatch, clock):
    """FINISHED, but sub-statement 5 has no result to read: nothing is read."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.describe_step(
                "FINISHED",
                fake.sub_statements(has_result=(False, False, False, True, False)),
            ),
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID
    assert script.operations() == OPERATIONS[:4]


def test_an_aborted_batch_is_cancelled_and_not_cancelled_again(
    aws_env, monkeypatch, clock
):
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.describe_step("ABORTED", fake.sub_statements(("ABORTED",) * 5)),
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.CANCELLED
    assert ("redshift-data", "cancel_statement") not in script.operations()


@pytest.mark.regression
def test_a_failed_batch_names_the_failed_sub_statements_query_id(
    aws_env, monkeypatch, clock
):
    """FAILED is unrecognised, vendor code FAILED, and the request id is the
    RedshiftQueryId of the sub-statement that failed (SYS_QUERY_HISTORY's
    id), not the batch's own."""
    failed = fake.describe_step(
        "FAILED",
        fake.sub_statements(("FINISHED",) * 4 + ("FAILED",)),
    )
    failed.response = {**failed.response, "RedshiftQueryId": 777}
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps() + [fake.batch_step(DIAGNOSTICS.sql), failed],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.UNRECOGNISED_WAREHOUSE_ERROR
    assert err.value.vendor_code == "FAILED"
    assert err.value.request_id == "9005"
    assert err.value.warehouse == "redshift"
    assert err.value.message == (
        "Amazon Redshift returned an error we don't recognise (code FAILED). "
        "See Troubleshooting in the warehouse analysis docs."
    )
    assert script.operations() == OPERATIONS[:4]
    assert err.value.__context__ is None


def test_a_poll_that_fails_stops_the_batch(aws_env, monkeypatch, clock):
    """The fake told to fail mid-poll: the batch may still be running, so it
    is cancelled once, and the poll's own failure is what is reported."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.Step(
                "redshift-data",
                "describe_statement",
                {"Id": fake.STATEMENT_ID},
                error=("InternalServerException", 500),
            ),
            fake.cancel_step(),
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.UNRECOGNISED_WAREHOUSE_ERROR
    assert err.value.vendor_code == "InternalServerException"
    assert script.operations()[-1] == ("redshift-data", "cancel_statement")


def test_a_refused_submit_sends_nothing_more(aws_env, monkeypatch, clock):
    """No statement id, nothing to poll or cancel."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.Step(
                "redshift-data",
                "batch_execute_statement",
                None,
                error=("ValidationException", 400),
            )
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.UNRECOGNISED_WAREHOUSE_ERROR
    assert err.value.vendor_code == "ValidationException"
    assert script.operations() == OPERATIONS[:3]


@pytest.mark.parametrize(
    "answer",
    [
        {"Id": "not-a-uuid"},
        {"Id": fake.STATEMENT_ID + "\n"},
        {},
    ],
    ids=["not-a-uuid", "trailing-newline", "none"],
)
def test_a_statement_id_that_is_not_a_uuid_is_refused_before_it_is_used(
    answer, aws_env, monkeypatch, clock
):
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [fake.Step("redshift-data", "batch_execute_statement", None, answer)],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID


def test_an_answer_about_another_statement_is_refused(aws_env, monkeypatch, clock):
    other = "00000000-0000-0000-0000-000000000000"
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.Step(
                "redshift-data",
                "describe_statement",
                {"Id": fake.STATEMENT_ID},
                {"Id": other, "Status": "FINISHED"},
            ),
            fake.cancel_step(),
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID


# -- the deadline ------------------------------------------------------------------


def _still_running(n: int) -> List[fake.Step]:
    running = fake.sub_statements(("FINISHED",) * 3 + ("STARTED",) * 2)
    return [fake.describe_step("STARTED", running) for _ in range(n)]


@pytest.mark.regression
def test_the_statements_limit_cancels_it_once(aws_env, monkeypatch, clock, caplog):
    """A batch that stays STARTED: at the connection's 10 s limit (not the
    operation's 1000 s) CancelStatement is sent once, with the statement's
    id, and the query fails with time_limit.  The polls 0.25, 0.5, 1 and 2 s
    apart reach 9.75 s at the eighth; the next pause would pass the limit."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [fake.batch_step(DIAGNOSTICS.sql, timeout_ms=20_000)]
        + _still_running(8)
        + [fake.cancel_step()],
    )
    started = clock()
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(fake.connection(query_timeout_seconds=10), clock), clock)
    script.assert_played()
    assert err.value.code is _C.TIME_LIMIT
    cancels = [c for c in script.calls if c[1] == "cancel_statement"]
    assert cancels == [("redshift-data", "cancel_statement", {"Id": fake.STATEMENT_ID})]
    assert clock() - started == pytest.approx(9.75)


def test_a_failed_cancel_still_reports_time_limit_and_logs_only_a_code(
    aws_env, monkeypatch, clock, caplog
):
    caplog.set_level(logging.DEBUG)
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [fake.batch_step(DIAGNOSTICS.sql, timeout_ms=20_000)]
        + _still_running(8)
        + [
            fake.Step(
                "redshift-data",
                "cancel_statement",
                {"Id": fake.STATEMENT_ID},
                error=("ValidationException", 400),
            )
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(fake.connection(query_timeout_seconds=10), clock), clock)
    script.assert_played()
    assert err.value.code is _C.TIME_LIMIT
    records = [
        r for r in caplog.records if r.getMessage().startswith("redshift cancel")
    ]
    assert [r.getMessage() for r in records] == [
        "redshift cancel failed: unrecognised_warehouse_error"
    ]
    assert fake.TEXT_SENTINEL not in caplog.text


def test_no_time_left_sends_nothing(aws_env, monkeypatch, clock):
    script = _script(monkeypatch, clock, [])
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock, seconds=5.5)
    assert err.value.code is _C.TIME_LIMIT
    assert script.calls == []


# -- the role's session -----------------------------------------------------------


@pytest.mark.regression
def test_the_role_is_asked_for_an_hour(aws_env, monkeypatch, clock):
    """Not STS's 900 s default: the connection's default limit (300 s) and the
    deployment's default ceiling (1800 s) both fit one session.  Both
    AssumeRole calls ask for 3600 s (the Stubber holds the exact parameters)."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps(seconds=3600)
        + fake.happy_batch(DIAGNOSTICS.sql, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW]),
    )
    _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert [c[2]["DurationSeconds"] for c in script.calls[:2]] == [3600, 3600]
    assert "ExternalId" not in script.calls[0][2]
    assert script.calls[1][2]["ExternalId"] == fake.EXTERNAL_ID


@pytest.mark.regression
@pytest.mark.parametrize(
    ("gap", "assumed"),
    [(200.0, 1), (3239.0, 1), (3240.0, 2), (3500.0, 2)],
    ids=["minutes-later", "just-covered", "just-not-covered", "nearly-run-out"],
)
def test_the_role_is_assumed_again_only_when_the_session_cannot_cover_the_next_statement(
    gap, assumed, aws_env, monkeypatch, clock
):
    """One adapter (one operation), two statements of the connection's 300 s.
    The first takes 0.25 s (one STARTED poll), so after ``gap`` the session
    has 3599.75 - gap seconds left: the second statement uses it while that
    covers its 300 s limit and the 60 s reserve, and otherwise the role is
    assumed again first (both calls), so nothing is signed with credentials
    that run out mid-statement."""
    batch = fake.happy_batch(DIAGNOSTICS.sql, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW])
    again = fake.assume_steps() if assumed == 2 else []
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + batch
        + again
        + fake.happy_batch(DIAGNOSTICS.sql, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW]),
    )
    adapter = fake.adapter(clock=clock)
    deadline = fake.deadline(clock, 20000)
    adapter.run_query(DIAGNOSTICS, deadline)
    clock.advance(gap)
    adapter.run_query(DIAGNOSTICS, deadline)
    script.assert_played()
    sts = [op for op in script.operations() if op[0] == "sts"]
    assert sts == [("sts", "assume_role")] * 2 * assumed
    if assumed == 2:
        assert script.operations() == OPERATIONS + OPERATIONS


@pytest.mark.regression
def test_a_statement_never_outlives_one_session(aws_env, monkeypatch, clock):
    """A connection limit above what a session covers (only possible if the
    operator raised WAREHOUSE_MAX_QUERY_TIMEOUT_SECONDS) is held to 3540 s:
    Redshift's own stop at 3550 s."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + fake.happy_batch(
            DIAGNOSTICS.sql, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW], timeout_ms=3_550_000
        ),
    )
    _run(
        fake.adapter(fake.connection(query_timeout_seconds=7200), clock),
        clock,
        seconds=100_000,
    )
    script.assert_played()
    assert redshift.MAX_STATEMENT_SECONDS == 3540
    assert redshift.SESSION_SECONDS == aws.ROLE_SESSION_MAX_SECONDS == 3600


@pytest.mark.regression
def test_a_slow_assume_role_shortens_the_statement_to_what_the_session_has_left(
    aws_env, monkeypatch, clock
):
    """The session runs out 3600 s after the first AssumeRole was asked for.
    STS takes 90 s to answer here, so 3510 s are left: the statement may run
    3510 - 60 = 3450 s (Redshift's own stop at 3460 s), not the 3540 s a fresh
    session would allow."""
    steps = fake.assume_steps()
    steps[0].seconds = 90.0
    script = _script(
        monkeypatch,
        clock,
        steps
        + fake.happy_batch(
            DIAGNOSTICS.sql, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW], timeout_ms=3_460_000
        ),
    )
    _run(
        fake.adapter(fake.connection(query_timeout_seconds=7200), clock),
        clock,
        seconds=100_000,
    )
    script.assert_played()


@pytest.mark.parametrize("seconds", [899, 3601, 0, True, 3600.0, "3600", None])
def test_a_session_length_outside_sts_range_is_refused_before_any_call(
    seconds, aws_env, monkeypatch, clock
):
    script = _script(monkeypatch, clock, [])
    with pytest.raises(WarehouseError) as err:
        aws.assume_role_session(
            fake.platform_session(),
            fake.ROLE_ARN,
            fake.EXTERNAL_ID,
            fake.REGION,
            warehouse="redshift",
            deadline=fake.deadline(clock),
            duration_seconds=seconds,
        )
    assert err.value.code is _C.INTERNAL
    assert script.calls == []


def test_a_role_that_admits_the_deployment_without_the_external_id_runs_nothing(
    aws_env, monkeypatch, clock
):
    script = _script(
        monkeypatch,
        clock,
        [
            fake.Step(
                "sts",
                "assume_role",
                {
                    "RoleArn": fake.ROLE_ARN,
                    "RoleSessionName": "controlvar-warehouse",
                    "DurationSeconds": 3600,
                },
                fake.ASSUMED,
            )
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.ROLE_TRUST_UNSAFE
    assert script.operations() == [("sts", "assume_role")]


# -- pagination and typed records ---------------------------------------------------


@pytest.mark.regression
def test_every_result_page_is_read(aws_env, monkeypatch, clock):
    """Two pages (NextToken): every row of both, in order."""
    names = ["variant", "units"]
    first = [[fake.string(f"v{i}"), fake.long(i)] for i in range(3)]
    second = [[fake.string(f"v{i}"), fake.long(i)] for i in range(3, 5)]
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.describe_step("FINISHED", fake.sub_statements()),
            fake.probe_step(),
            fake.result_step(5, names, first, token_out="page-2"),
            fake.result_step(5, names, second, token_in="page-2"),
        ],
    )
    result = _run(fake.adapter(clock=clock), clock)
    assert len(result.rows) == 5
    assert [r["variant"] for r in result.rows] == ["v0", "v1", "v2", "v3", "v4"]
    script.assert_played()


def test_more_pages_than_a_result_can_need_are_refused(aws_env, monkeypatch, clock):
    names = ["n"]
    pages = [
        fake.result_step(
            5,
            names,
            [[fake.long(i)]],
            token_in=f"t{i}" if i else None,
            token_out=f"t{i + 1}",
        )
        for i in range(redshift.MAX_RESULT_PAGES)
    ]
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.describe_step("FINISHED", fake.sub_statements()),
            fake.probe_step(),
            *pages,
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID


@pytest.mark.regression
@pytest.mark.parametrize(
    "value",
    [
        {"doubleValue": 0.30000000000000004},
        {"booleanValue": True},
        {"blobValue": b"x"},
        {"isNull": False},
    ],
    ids=["double", "boolean", "blob", "not-null"],
)
def test_a_value_that_is_not_null_a_long_or_a_string_is_refused(
    value, aws_env, monkeypatch, clock
):
    """Our SQL returns every float as text: a doubleValue where the string
    is due would carry the API's own rendering of the number."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.describe_step("FINISHED", fake.sub_statements()),
            fake.probe_step(),
            fake.result_step(5, ["k", "n"], [[value, fake.long(1)]]),
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID


@pytest.mark.parametrize(
    ("names", "metadata"),
    [
        (["n", "N"], True),
        (['bad"name'], True),
        (["n"], False),
    ],
    ids=["same-name-twice", "not-an-identifier", "no-metadata"],
)
def test_result_columns_of_another_shape_are_refused(
    names, metadata, aws_env, monkeypatch, clock
):
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.describe_step("FINISHED", fake.sub_statements()),
            fake.probe_step(),
            fake.result_step(
                5, names, [[fake.long(1)] * len(names)], metadata=metadata
            ),
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID


def test_values_decode_to_none_int_and_str():
    assert redshift.decode_records(
        [[fake.NULL, fake.long(7), fake.string("0.5")]], 3
    ) == [[None, 7, "0.5"]]
    for bad in ([[fake.long(1)]], [[fake.long(1), fake.long(2), fake.long(3)]]):
        with pytest.raises(WarehouseError):
            redshift.decode_records(bad, 2)


# -- errors by Code -------------------------------------------------------------------

#: What each Code maps to: the model's every error shape, and the unmodelled ones.
#: ``None``: by HTTP status (here 400, so unrecognised), the Code kept.
EXPECTED_CODES: Dict[str, Any] = {
    "ActiveSessionsExceededException": _C.WAREHOUSE_BUSY,
    "ActiveStatementsExceededException": _C.WAREHOUSE_BUSY,
    "ActiveWaitingRequestsExceededException": _C.WAREHOUSE_BUSY,
    "QueryTimeoutException": _C.TIME_LIMIT,
    "ResourceNotFoundException": _C.UNRECOGNISED_WAREHOUSE_ERROR,
    "BatchExecuteStatementException": None,
    "DatabaseConnectionException": None,
    "ExecuteStatementException": None,
    "InternalServerException": None,
    "ValidationException": None,
    "ThrottlingException": _C.WAREHOUSE_BUSY,
    "AccessDeniedException": _C.PERMISSION_DENIED,
}


def _model_codes() -> List[str]:
    client = boto3.session.Session(
        aws_access_key_id="x", aws_secret_access_key="x"
    ).client("redshift-data", region_name=fake.REGION)
    return sorted(shape.name for shape in client.meta.service_model.error_shapes)


def test_every_code_the_model_has_is_decided():
    """A new error shape in botocore's model fails here until it is mapped."""
    modelled = _model_codes()
    assert sorted(set(EXPECTED_CODES) - set(UNMODELLED_CODES)) == modelled
    assert set(UNMODELLED_CODES).isdisjoint(modelled)
    assert set(REDSHIFT_CODES) <= set(EXPECTED_CODES)


@pytest.mark.regression
@pytest.mark.parametrize("stage", ["submit", "poll"])
@pytest.mark.parametrize("code", sorted(EXPECTED_CODES))
def test_errors_map_by_their_code(code, stage, aws_env, monkeypatch, clock):
    expected = EXPECTED_CODES[code] or _C.UNRECOGNISED_WAREHOUSE_ERROR
    error = fake.Step(
        "redshift-data",
        "batch_execute_statement" if stage == "submit" else "describe_statement",
        None,
        error=(code, 400),
    )
    steps = fake.assume_steps()
    if stage == "poll":
        steps += [fake.batch_step(DIAGNOSTICS.sql), error, fake.cancel_step()]
    else:
        steps += [error]
    script = _script(monkeypatch, clock, steps)
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is expected
    assert err.value.vendor_code == code
    assert err.value.warehouse == "redshift"
    assert fake.TEXT_SENTINEL not in repr(err.value) + err.value.message


def test_a_missing_statement_is_never_a_missing_table(aws_env, monkeypatch, clock):
    """ResourceNotFoundException names our statement id: whatever its status,
    it is not object_not_found (which the source check reads as "no table")."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.Step(
                "redshift-data",
                "describe_statement",
                None,
                error=("ResourceNotFoundException", 404),
            ),
            fake.cancel_step(),
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.UNRECOGNISED_WAREHOUSE_ERROR


# -- no vendor text ---------------------------------------------------------------------


def _surfaces(err: WarehouseError, caplog, *more: Any) -> str:
    return "\n".join(
        [
            str(err),
            repr(err),
            err.message,
            repr(err.to_body()),
            repr(err.log_fields()),
            caplog.text,
            "\n".join(repr(r.__dict__) for r in caplog.records),
            *(repr(m) for m in more),
        ]
    )


@pytest.mark.regression
def test_the_error_text_of_a_failed_statement_is_never_read(
    aws_env, monkeypatch, clock, caplog
):
    """An Error a text classifier would map ("permission denied for relation
    x") is still FAILED and unrecognised; neither it nor a sentinel in it
    reaches the error, its body, its log fields, a log record or a repr."""
    caplog.set_level(logging.DEBUG)
    text = f"permission denied for relation x {fake.TEXT_SENTINEL}"
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(DIAGNOSTICS.sql),
            fake.describe_step(
                "FAILED",
                fake.sub_statements(("FINISHED",) * 4 + ("FAILED",)),
                error_text=text,
            ),
        ],
    )
    adapter = fake.adapter(clock=clock)
    with pytest.raises(WarehouseError) as err:
        _run(adapter, clock)
    script.assert_played()
    assert err.value.code is _C.UNRECOGNISED_WAREHOUSE_ERROR
    assert err.value.vendor_code == "FAILED"
    surfaces = _surfaces(err.value, caplog, adapter)
    assert fake.TEXT_SENTINEL not in surfaces
    assert "permission denied" not in surfaces


def test_only_the_fields_the_connector_uses_are_kept_from_describe_statement():
    answer = {
        "Id": fake.STATEMENT_ID,
        "Status": "FAILED",
        "Error": fake.TEXT_SENTINEL,
        "QueryString": fake.TEXT_SENTINEL,
        "SubStatements": [
            {
                "Id": f"{fake.STATEMENT_ID}:1",
                "Status": "FAILED",
                "Error": fake.TEXT_SENTINEL,
                "QueryString": fake.TEXT_SENTINEL,
                "RedshiftQueryId": 5,
                "HasResultSet": False,
            }
        ],
    }
    kept = described(answer, fake.STATEMENT_ID)
    assert fake.TEXT_SENTINEL not in repr(kept)
    assert kept == {
        "Status": "FAILED",
        "SubStatements": [
            {
                "Id": f"{fake.STATEMENT_ID}:1",
                "Status": "FAILED",
                "HasResultSet": False,
                "RedshiftQueryId": 5,
            }
        ],
    }


def test_an_aws_error_message_reaches_nothing(aws_env, monkeypatch, clock, caplog):
    caplog.set_level(logging.DEBUG)
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.Step(
                "redshift-data",
                "batch_execute_statement",
                None,
                error=("ValidationException", 400),
            )
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert fake.TEXT_SENTINEL not in _surfaces(err.value, caplog)
    assert err.value.__context__ is None


def test_the_roles_credentials_reach_no_log_and_no_repr(
    aws_env, monkeypatch, clock, caplog
):
    caplog.set_level(logging.DEBUG)
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + fake.happy_batch(DIAGNOSTICS.sql, DIAGNOSTIC_NAMES, [DIAGNOSTIC_ROW]),
    )
    adapter = fake.adapter(clock=clock)
    result = _run(adapter, clock)
    script.assert_played()
    text = caplog.text + "\n".join(repr(r.__dict__) for r in caplog.records)
    assert fake.TEMP_SENTINEL not in text
    for shown in (adapter, adapter.connection, result):
        assert fake.TEMP_SENTINEL not in repr(shown)
    # Positive control: the adapter does hold them, signing with them.
    held = adapter._role[0].get_credentials()
    assert held.token == "token-" + fake.TEMP_SENTINEL


# -- the connection test and column listing -------------------------------------------


def test_the_connection_test_reads_no_table(aws_env, monkeypatch, clock):
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step("SELECT 1 AS connected", timeout_ms=35_000),
            fake.describe_step("FINISHED", fake.sub_statements()),
            fake.probe_step(),
            fake.result_step(5, ["connected"], [[fake.long(1)]]),
        ],
    )
    check = fake.adapter(clock=clock).check_connection(fake.deadline(clock, 30))
    script.assert_played()
    assert (check.probe, check.time_zone, check.warehouse) == (
        "0.30000000000000004",
        "UTC",
        "analytics",
    )


def test_a_connection_test_whose_statement_answers_otherwise_is_refused(
    aws_env, monkeypatch, clock
):
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step("SELECT 1 AS connected", timeout_ms=35_000),
            fake.describe_step("FINISHED", fake.sub_statements()),
            fake.probe_step(),
            fake.result_step(5, ["connected"], [[fake.long(2)]]),
        ],
    )
    with pytest.raises(WarehouseError) as err:
        fake.adapter(clock=clock).check_connection(fake.deadline(clock, 30))
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID


def _describe_table(target, columns, *, token_in=None, token_out=None) -> fake.Step:
    expected: Dict[str, Any] = {
        **target,
        "Schema": "analytics",
        "Table": "exposures",
        "MaxResults": 1000,
    }
    if token_in is not None:
        expected["NextToken"] = token_in
    answer: Dict[str, Any] = {
        "TableName": "exposures",
        "ColumnList": [{"name": n, "typeName": t} for n, t in columns],
    }
    if token_out is not None:
        answer["NextToken"] = token_out
    return fake.Step("redshift-data", "describe_table", expected, answer)


@pytest.mark.parametrize(
    ("conn", "target"),
    [(fake.connection, fake.SERVERLESS), (fake.provisioned, fake.PROVISIONED)],
    ids=["serverless", "provisioned"],
)
def test_table_columns_from_describe_table(conn, target, aws_env, monkeypatch, clock):
    """Metadata only, every page; no statement runs."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            _describe_table(
                target,
                [("user_id", "varchar"), ("variant", "varchar")],
                token_out="more",
            ),
            _describe_table(
                target,
                [("exposed_at", "timestamptz"), ("amount", "numeric(18,2)")],
                token_in="more",
            ),
        ],
    )
    columns = fake.adapter(conn(), clock).table_columns(
        ("analytics", "exposures"), fake.deadline(clock, 30)
    )
    script.assert_played()
    assert columns == (
        ("user_id", "varchar"),
        ("variant", "varchar"),
        ("exposed_at", "timestamptz"),
        ("amount", "numeric(18,2)"),
    )
    client = clients.build_client(
        clients.ConnectionSpec(
            "redshift",
            {
                "region": fake.REGION,
                "role_arn": fake.ROLE_ARN,
                "workgroup_name": "analytics",
                "database": "dev",
            },
            300,
            None,
            external_id=fake.EXTERNAL_ID,
        )
    )
    assert client.is_time_type("timestamptz") and client.is_time_type("timestamp")
    assert client.is_numeric_type("numeric(18,2)") and client.is_numeric_type("float8")
    assert not client.is_time_type("date") and not client.is_numeric_type("varchar")


def test_a_table_with_no_columns_is_not_found(aws_env, monkeypatch, clock):
    script = _script(
        monkeypatch, clock, fake.assume_steps() + [_describe_table(fake.SERVERLESS, [])]
    )
    with pytest.raises(WarehouseError) as err:
        fake.adapter(clock=clock).table_columns(
            ("analytics", "exposures"), fake.deadline(clock, 30)
        )
    script.assert_played()
    assert err.value.code is _C.OBJECT_NOT_FOUND


@pytest.mark.parametrize(
    "column", [('user"id', "varchar"), ("user_id", "var;char"), ("user_id", "")]
)
def test_a_column_listing_of_another_shape_is_refused(
    column, aws_env, monkeypatch, clock
):
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps() + [_describe_table(fake.SERVERLESS, [column])],
    )
    with pytest.raises(WarehouseError) as err:
        fake.adapter(clock=clock).table_columns(
            ("analytics", "exposures"), fake.deadline(clock, 30)
        )
    script.assert_played()
    assert err.value.code is _C.RESULT_INVALID


@pytest.mark.parametrize(
    "table",
    [
        ("dev", "analytics", "exposures"),
        ("exposures",),
        ("analytics", 'exp"osures'),
        ("analytics", "exposures\n"),
    ],
)
def test_a_table_reference_is_checked_before_anything_is_sent(
    table, aws_env, monkeypatch, clock
):
    script = _script(monkeypatch, clock, [])
    with pytest.raises(WarehouseQueryRefused) as refused:
        fake.adapter(clock=clock).table_columns(table, fake.deadline(clock, 30))
    assert refused.value.code == "invalid_identifier"
    assert script.calls == []


# -- the connection and the client --------------------------------------------------


@pytest.mark.parametrize(
    "overrides",
    [
        {"cluster_identifier": "analytics-cluster"},
        {"db_user": "reader"},
        {"workgroup_name": None},
        {"workgroup_name": None, "cluster_identifier": "analytics-cluster"},
        {"workgroup_name": "Analytics"},
        {"workgroup_name": "analytics\n"},
        {"database": "Dev"},
        {"region": "nowhere-1"},
        {"region": "us-gov-west-1"},
        {"query_timeout_seconds": 0},
        {"query_timeout_seconds": True},
        {"role_arn": None},
    ],
)
def test_a_connection_outside_the_schemas_rules_is_internal(overrides):
    with pytest.raises(WarehouseError) as err:
        fake.connection(**overrides)
    assert err.value.code is _C.INTERNAL


def test_the_client_factory_builds_redshift_and_calls_nothing(
    aws_env, monkeypatch, clock
):
    script = _script(monkeypatch, clock, [])
    spec = clients.ConnectionSpec(
        "redshift",
        {
            "region": fake.REGION,
            "role_arn": fake.ROLE_ARN,
            "cluster_identifier": "analytics-cluster",
            "db_user": "controlvar_reader",
            "database": "dev",
        },
        600,
        None,
        external_id=fake.EXTERNAL_ID,
    )
    client = clients.build_client(spec)
    assert client.warehouse_type == "redshift"
    assert client.dialect is REDSHIFT_SQL
    assert isinstance(client.adapter, RedshiftAdapter)
    assert client.adapter.connection.target() == fake.PROVISIONED
    assert client.adapter.connection.query_timeout_seconds == 600
    assert script.calls == []
    for broken in (
        {"external_id": None},
        {"credential": b"a stored secret"},
    ):
        with pytest.raises(WarehouseError) as err:
            clients.build_client(
                clients.ConnectionSpec(
                    "redshift",
                    spec.parameters,
                    600,
                    None,
                    **{"external_id": fake.EXTERNAL_ID, **broken},
                )
            )
        assert err.value.code is _C.INTERNAL


def test_only_generated_redshift_statements_run(aws_env, monkeypatch, clock):
    script = _script(monkeypatch, clock, [])
    adapter = fake.adapter(clock=clock)
    for built in (
        build_case("snowflake", "diagnostics"),
        BuiltQuery(kind="preview", dialect="athena", sql="SELECT 1"),
        "SELECT 1",
    ):
        with pytest.raises(WarehouseError) as err:
            adapter.run_query(built, fake.deadline(clock))
        assert err.value.code is _C.INTERNAL
    assert script.calls == []


# -- the Data API's endpoint ----------------------------------------------------------


def _session() -> Any:
    return fake.platform_session()


def test_the_data_apis_regional_endpoint_is_the_only_redshift_host(aws_env):
    client = aws.make_client(
        _session(), "redshift-data", "eu-west-1", warehouse="redshift"
    )
    assert client.meta.endpoint_url == "https://redshift-data.eu-west-1.amazonaws.com"
    for service, region in (
        ("redshift", "eu-west-1"),  # the control plane
        ("redshift-serverless", "eu-west-1"),
        ("redshift-data", "nowhere-1"),
        ("redshift-data", "cn-north-1"),
    ):
        with pytest.raises(WarehouseError) as err:
            aws.make_client(_session(), service, region, warehouse="redshift")
        assert err.value.code is _C.DESTINATION_NOT_ALLOWED


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("AWS_USE_FIPS_ENDPOINT", "true"),
        ("AWS_ENDPOINT_URL", "https://127.0.0.1:4566"),
        ("AWS_ENDPOINT_URL_REDSHIFT_DATA", "https://127.0.0.1:4566"),
    ],
)
def test_another_data_api_endpoint_is_refused(name, value, aws_env, monkeypatch):
    monkeypatch.setenv(name, value)
    with pytest.raises(WarehouseError) as err:
        aws.make_client(_session(), "redshift-data", "eu-west-1", warehouse="redshift")
    assert err.value.code is _C.DESTINATION_NOT_ALLOWED


# -- a run, end to end ------------------------------------------------------------------


def test_a_run_reads_the_rows_the_runner_parses(aws_env, monkeypatch, clock):
    """The diagnostics and one proportion metric through the runner: counts
    as longValue, the serialised floats as stringValue."""
    from modules.backend.tests.live_warehouse import harness

    client = clients.build_client(
        clients.ConnectionSpec(
            "redshift",
            {
                "region": fake.REGION,
                "role_arn": fake.ROLE_ARN,
                "workgroup_name": "analytics",
                "database": "dev",
            },
            300,
            None,
            external_id=fake.EXTERNAL_ID,
        )
    )
    client = type(client)(
        client.warehouse_type,
        fake.adapter(client.adapter.connection, clock),
        client.dialect,
        client.time_types,
        client.numeric_types,
    )
    identity = {n: n for n in harness.EXPOSURE_COLUMNS + harness.EVENT_COLUMNS}
    columns = {
        "exposures": {n: identity[n] for n in harness.EXPOSURE_COLUMNS},
        "events": {n: identity[n] for n in harness.EVENT_COLUMNS},
    }
    plan = harness.run_plan(
        client, ("analytics", "exposures"), ("analytics", "events"), columns
    )
    metric_names = [
        "variant",
        "n",
        "n_converted",
        "k",
        "sum_d",
        "sum_d2",
        "metric_rows_in_window",
        "metric_rows_matched",
        "null_value_rows",
    ]

    def metric_row(variant, n, converted, sum_d, sum_d2):
        return [
            fake.string(variant),
            fake.long(n),
            fake.long(converted),
            fake.string("0.5"),
            fake.string(sum_d),
            fake.string(sum_d2),
            fake.long(200),
            fake.long(180),
            fake.long(0),
        ]

    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + fake.happy_batch(
            plan.diagnostics.sql,
            DIAGNOSTIC_NAMES,
            [[fake.long(n) for n in (200, 0, 200, 0, 2)]],
        )
        + fake.happy_batch(
            plan.metrics[0].query.sql,
            metric_names,
            [
                metric_row("control", 100, 40, "-10", "24"),
                metric_row("treatment", 100, 60, "10", "24"),
            ],
        ),
    )
    values = harness.execute_plan(plan, fake.deadline(clock, 5000))
    script.assert_played()
    metric = values["results"]["metrics"][0]
    assert metric["computed"] is True
    assert values["results"]["diagnostics"]["units"] == 200
    assert [m["statement_handle"] for m in values["job_metadata"]] == [
        fake.STATEMENT_ID
    ] * 2
