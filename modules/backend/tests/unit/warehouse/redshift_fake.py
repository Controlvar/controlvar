"""A scripted AWS for the Redshift connector's tests: real clients under Stubber.

Every client :func:`~modules.backend.app.warehouse.aws.make_client` builds --
one per call, as :func:`~modules.backend.app.warehouse.aws.call` builds them --
is the real botocore client for its service, with the connector's guard
installed, under a :class:`botocore.stub.Stubber` holding the next step of a
script.  So a parameter the service's model does not have, a value other than
the one the step expects, a call the script does not expect, or the wrong
operation, each fails the test.  The Stubber answers before botocore's HTTP
layer: nothing is sent and no name is looked up.

Each answer is checked by the Stubber against the installed botocore model,
so it has the shape the real service's answers have.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import boto3
from botocore.exceptions import StubResponseError
from botocore.stub import Stubber

from modules.backend.app.warehouse import aws
from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.app.warehouse.redshift import RedshiftAdapter, RedshiftConnection
from modules.backend.tests.unit.warehouse.conftest import FakeClock

REGION = "eu-west-1"
ROLE_ARN = "arn:aws:iam::123456789012:role/controlvar-warehouse/analysis-reader"
EXTERNAL_ID = "exp-" + "0123456789abcdef" * 2
STATEMENT_ID = "d9b6c0c9-0747-4bf4-b142-e8883122f766"
#: Stands in for the role's temporary credentials.
TEMP_SENTINEL = "RSTEMPCREDSENTINEL7Q"
#: Stands in for any text AWS or Redshift sends (an error message, ``Error``).
TEXT_SENTINEL = "UPSTREAM-SENTINEL-rs-3e9d"

#: The probe, written out here rather than imported: a change to it fails.
PROBE = (
    "SELECT CAST(CAST(0.1 AS DOUBLE PRECISION) + CAST(0.2 AS DOUBLE PRECISION)"
    " AS VARCHAR) AS probe, current_setting('timezone') AS tz"
)
SETUP = ["SET timezone TO 'UTC'", "SET extra_float_digits TO 2"]
NAME = "controlvar-analysis"

#: Where a Serverless and a provisioned connection's statements run.
SERVERLESS = {"Database": "dev", "WorkgroupName": "analytics"}
PROVISIONED = {
    "Database": "dev",
    "ClusterIdentifier": "analytics-cluster",
    "DbUser": "controlvar_reader",
}

ASSUMED = {
    "Credentials": {
        "AccessKeyId": "ASIA" + TEMP_SENTINEL,
        "SecretAccessKey": "secret-" + TEMP_SENTINEL,
        "SessionToken": "token-" + TEMP_SENTINEL,
        "Expiration": datetime(2026, 11, 13, 19, 0, tzinfo=timezone.utc),
    },
    "AssumedRoleUser": {
        "AssumedRoleId": "AROA0123456789ABCDEFG:controlvar-warehouse",
        "Arn": "arn:aws:sts::123456789012:assumed-role/analysis-reader/x",
    },
}


def connection(**overrides: Any) -> RedshiftConnection:
    values: Dict[str, Any] = {
        "region": REGION,
        "role_arn": ROLE_ARN,
        "external_id": EXTERNAL_ID,
        "database": "dev",
        "query_timeout_seconds": 300,
        "workgroup_name": "analytics",
    }
    values.update(overrides)
    return RedshiftConnection(**values)


def provisioned(**overrides: Any) -> RedshiftConnection:
    return connection(
        workgroup_name=None,
        cluster_identifier="analytics-cluster",
        db_user="controlvar_reader",
        **overrides,
    )


def platform_session() -> Any:
    """This deployment's identity, as dummies: nothing here can sign for real."""
    return boto3.session.Session(
        aws_access_key_id="AKIDUMMYDUMMYDUMMY00",
        aws_secret_access_key="dummy-secret",
        region_name=REGION,
    )


@dataclass
class Step:
    """One AWS call the script expects, and its answer or error."""

    service: str
    operation: str
    expected: Optional[Mapping[str, Any]]
    response: Optional[Mapping[str, Any]] = None
    error: Optional[Tuple[str, int]] = None
    #: Seconds the fake clock moves while the call is answered.
    seconds: float = 0.0


class _Recording:
    """Records the operation and parameters of every call, then makes it."""

    def __init__(self, client: Any, script: "AwsScript", service: str) -> None:
        self._client = client
        self._script = script
        self._service = service

    def __getattr__(self, name: str) -> Any:
        def operation(**params: Any) -> Any:
            self._script.calls.append((self._service, name, params))
            mismatch = None
            try:
                return getattr(self._client, name)(**params)
            except StubResponseError as exc:
                # A BotoCoreError, which aws.call would report as
                # ``unreachable``: raised as a plain AssertionError instead,
                # which nothing in the connector catches, so the test fails
                # on the mismatch itself.
                mismatch = f"{name}: {exc}"
            self._script.mismatches.append(mismatch)
            raise AssertionError(mismatch)

        return operation


class AwsScript:
    """Answers each client make_client builds with the next step."""

    def __init__(
        self,
        monkeypatch: Any,
        steps: Sequence[Step] = (),
        clock: Optional[FakeClock] = None,
    ) -> None:
        self.steps: List[Step] = list(steps)
        self.calls: List[Tuple[str, str, Dict[str, Any]]] = []
        self.mismatches: List[str] = []
        self.stubbers: List[Stubber] = []
        self.clock = clock
        real_make = aws.make_client
        script = self

        def build(session: Any, service: str, region: str, **kwargs: Any) -> Any:
            client = real_make(session, service, region, **kwargs)
            assert script.steps, f"an AWS call the script does not expect ({service})"
            step = script.steps.pop(0)
            assert (service, region) == (step.service, REGION), (
                f"the script expects {step.service} {step.operation} in {REGION}; "
                f"a {service} client was built in {region}"
            )
            stubber = Stubber(client)
            if step.error is not None:
                code, status = step.error
                stubber.add_client_error(
                    step.operation,
                    service_error_code=code,
                    service_message=f"{TEXT_SENTINEL} {code} message",
                    http_status_code=status,
                    expected_params=step.expected,
                )
            else:
                stubber.add_response(
                    step.operation, dict(step.response or {}), step.expected
                )
            stubber.activate()
            script.stubbers.append(stubber)
            if script.clock is not None and step.seconds:
                script.clock.advance(step.seconds)
            return _Recording(client, script, service)

        monkeypatch.setattr(aws, "make_client", build)

    def extend(self, steps: Sequence[Step]) -> None:
        self.steps.extend(steps)

    def operations(self) -> List[Tuple[str, str]]:
        return [(service, name) for service, name, _ in self.calls]

    def assert_played(self) -> None:
        """Every step was called, as expected, and nothing else was."""
        assert self.mismatches == []
        assert self.steps == [], [(s.service, s.operation) for s in self.steps]
        for stubber in self.stubbers:
            stubber.assert_no_pending_responses()


# -- steps --------------------------------------------------------------------


def assume_steps(seconds: int = 3600) -> List[Step]:
    """The two AssumeRole calls: without the external ID (refused), then with it."""
    probe = {
        "RoleArn": ROLE_ARN,
        "RoleSessionName": "controlvar-warehouse",
        "DurationSeconds": seconds,
    }
    return [
        Step("sts", "assume_role", probe, error=("AccessDenied", 403)),
        Step("sts", "assume_role", {**probe, "ExternalId": EXTERNAL_ID}, ASSUMED),
    ]


def batch_step(
    select_sql: str,
    timeout_ms: int = 310_000,
    target: Mapping[str, str] = SERVERLESS,
    *,
    statement_id: str = STATEMENT_ID,
) -> Step:
    """The one BatchExecuteStatement, exactly as it must be sent."""
    return Step(
        "redshift-data",
        "batch_execute_statement",
        {
            "Sqls": [
                *SETUP,
                f"SET statement_timeout TO {timeout_ms}",
                PROBE,
                select_sql,
            ],
            "ExecutionMode": "TRANSACTION",
            "ResultFormat": "JSON",
            "StatementName": NAME,
            **target,
        },
        {"Id": statement_id},
    )


def sub_statements(
    statuses: Sequence[str] = ("FINISHED",) * 5,
    *,
    statement_id: str = STATEMENT_ID,
    has_result: Sequence[bool] = (False, False, False, True, True),
) -> List[Dict[str, Any]]:
    return [
        {
            "Id": f"{statement_id}:{n}",
            "Status": status,
            "HasResultSet": result,
            "RedshiftQueryId": 9000 + n,
        }
        for n, (status, result) in enumerate(zip(statuses, has_result), start=1)
    ]


def describe_step(
    status: str,
    subs: Optional[List[Dict[str, Any]]] = None,
    *,
    statement_id: str = STATEMENT_ID,
    seconds: float = 0.0,
    error_text: Optional[str] = None,
) -> Step:
    answer: Dict[str, Any] = {"Id": statement_id, "Status": status}
    if subs is not None:
        answer["SubStatements"] = subs
    if error_text is not None:
        answer["Error"] = error_text
        for sub in answer.get("SubStatements", []):
            if sub.get("Status") == "FAILED":
                sub["Error"] = error_text
    return Step(
        "redshift-data",
        "describe_statement",
        {"Id": statement_id},
        answer,
        seconds=seconds,
    )


def string(value: str) -> Dict[str, Any]:
    return {"stringValue": value}


def long(value: int) -> Dict[str, Any]:
    return {"longValue": value}


NULL = {"isNull": True}


def result_step(
    index: int,
    names: Sequence[str],
    records: Sequence[Sequence[Mapping[str, Any]]],
    *,
    token_in: Optional[str] = None,
    token_out: Optional[str] = None,
    statement_id: str = STATEMENT_ID,
    metadata: bool = True,
) -> Step:
    expected: Dict[str, Any] = {"Id": f"{statement_id}:{index}"}
    if token_in is not None:
        expected["NextToken"] = token_in
    answer: Dict[str, Any] = {"Records": [list(r) for r in records]}
    if metadata:
        answer["ColumnMetadata"] = [{"name": n, "typeName": "varchar"} for n in names]
    if token_out is not None:
        answer["NextToken"] = token_out
    return Step("redshift-data", "get_statement_result", expected, answer)


def probe_step(
    text: str = "0.30000000000000004",
    zone: str = "UTC",
    *,
    statement_id: str = STATEMENT_ID,
) -> Step:
    return result_step(
        4, ["probe", "tz"], [[string(text), string(zone)]], statement_id=statement_id
    )


def cancel_step(*, statement_id: str = STATEMENT_ID) -> Step:
    return Step(
        "redshift-data", "cancel_statement", {"Id": statement_id}, {"Status": True}
    )


def happy_batch(
    select_sql: str,
    names: Sequence[str],
    records: Sequence[Sequence[Mapping[str, Any]]],
    *,
    timeout_ms: int = 310_000,
    target: Mapping[str, str] = SERVERLESS,
) -> List[Step]:
    """A batch that runs, is polled twice, finishes, and is read."""
    return [
        batch_step(select_sql, timeout_ms, target),
        describe_step("STARTED", sub_statements(("FINISHED",) * 3 + ("STARTED",) * 2)),
        describe_step("FINISHED", sub_statements()),
        probe_step(),
        result_step(5, names, records),
    ]


def adapter(
    conn: Optional[RedshiftConnection] = None, clock: Optional[FakeClock] = None
) -> RedshiftAdapter:
    clock = clock or FakeClock()

    def sleeper(seconds: float) -> None:
        clock.advance(seconds)

    return RedshiftAdapter(
        conn or connection(),
        platform_session=platform_session(),
        resolver=lambda host, port: [("93.184.216.34", port)],
        monotonic=clock,
        sleeper=sleeper,
    )


def deadline(clock: FakeClock, seconds: float = 1000.0) -> Deadline:
    return Deadline(seconds, clock=clock)
