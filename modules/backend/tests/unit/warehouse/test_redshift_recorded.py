"""The Amazon Redshift connector against answers recorded on a real account.

Each file under ``recorded/redshift`` is an answer the Redshift Data API gave
in the real-account check, run ``wl-redshift-20261118T011715Z-6b77b53f``.
Here the connector reads those answers (through the same scripted client as
``test_redshift.py``) and must reach the outcome the run recorded: so a
change to how it reads a real answer fails here, not only in a live run.

Only the two time stamps of a ``DescribeStatement`` answer are dropped: the
recording stores them as text, which the client's model does not accept.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

import pytest

from modules.backend.app.warehouse.errors import WarehouseError, WarehouseErrorCode
from modules.backend.tests.unit.warehouse import redshift_fake as fake
from modules.backend.tests.unit.warehouse.test_redshift import (
    DIAGNOSTICS,
    _run,
    _script,
    aws_env,
    clock,
)

pytestmark = pytest.mark.unit

_C = WarehouseErrorCode

RECORDED = Path(__file__).parent / "recorded" / "redshift"


def _recorded(name: str) -> tuple[int, Dict[str, Any]]:
    data = json.loads((RECORDED / name).read_text(encoding="utf-8"))
    return data["status"], _without_times(data["body"])


def _without_times(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_times(item)
            for key, item in value.items()
            if key not in {"CreatedAt", "UpdatedAt"}
        }
    if isinstance(value, list):
        return [_without_times(item) for item in value]
    return value


def _describe(name: str) -> tuple[str, fake.Step]:
    _, body = _recorded(name)
    statement_id = body["Id"]
    step = fake.Step("redshift-data", "describe_statement", {"Id": statement_id}, body)
    return statement_id, step


def _result(name: str, statement_id: str, index: int) -> fake.Step:
    _, body = _recorded(name)
    return fake.Step(
        "redshift-data",
        "get_statement_result",
        {"Id": f"{statement_id}:{index}"},
        body,
    )


def test_a_recorded_connection_test_passes(aws_env, monkeypatch, clock):
    """A real FINISHED batch, its probe and its result: the test passes, and
    the probe read back as the digits and the zone the connector requires."""
    statement_id, finished = _describe("real_batch_finished.json")
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.batch_step(
                "SELECT 1 AS connected", timeout_ms=35_000, statement_id=statement_id
            ),
            finished,
            _result("real_wire_probe_result.json", statement_id, 4),
            _result("real_connection_test_result.json", statement_id, 5),
        ],
    )
    check = fake.adapter(clock=clock).check_connection(fake.deadline(clock, 30))
    script.assert_played()
    assert (check.probe, check.time_zone) == ("0.30000000000000004", "UTC")


def test_a_recorded_failed_statement_names_the_failed_sub_statement(
    aws_env, monkeypatch, clock
):
    """A real FAILED batch (a table the user may not read), as the connector's
    poll reads it: five sub-statements, the fifth FAILED. Unrecognised, vendor
    code FAILED, and the request id is the failed sub-statement's Data API id.
    RedshiftQueryId was 0 on it, so it could not have named the statement."""
    statement_id, failed = _describe("real_failed_batch.json")
    assert failed.response["RedshiftQueryId"] == 0
    assert [s["Status"] for s in failed.response["SubStatements"]] == [
        "FINISHED",
        "FINISHED",
        "FINISHED",
        "FINISHED",
        "FAILED",
    ]
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [fake.batch_step(DIAGNOSTICS.sql, statement_id=statement_id), failed],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.UNRECOGNISED_WAREHOUSE_ERROR
    assert err.value.vendor_code == "FAILED"
    assert err.value.request_id == f"{statement_id}:5"


def test_a_recorded_describe_of_the_request_id_answers_failed():
    """DescribeStatement of ``<id>:5`` (what an operator is told to run): its
    Status is FAILED and it lists exactly that sub-statement."""
    statement_id, step = _describe("real_describe_request_id.json")
    assert step.response["Status"] == "FAILED"
    assert [s["Id"] for s in step.response["SubStatements"]] == [f"{statement_id}:5"]


def test_a_recorded_missing_workgroup_is_unrecognised_with_its_code(
    aws_env, monkeypatch, clock
):
    """A workgroup that does not exist: the Data API's ValidationException."""
    status, body = _recorded("real_missing_workgroup.json")
    code = body["Error"]["Code"]
    assert (status, code) == (400, "ValidationException")
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps()
        + [
            fake.Step(
                "redshift-data", "batch_execute_statement", None, error=(code, status)
            )
        ],
    )
    with pytest.raises(WarehouseError) as err:
        _run(fake.adapter(clock=clock), clock)
    script.assert_played()
    assert err.value.code is _C.UNRECOGNISED_WAREHOUSE_ERROR
    assert err.value.vendor_code == "ValidationException"


def _describe_table(name: str) -> fake.Step:
    _, body = _recorded(name)
    return fake.Step(
        "redshift-data",
        "describe_table",
        {
            **fake.SERVERLESS,
            "Schema": "live_check",
            "Table": body["TableName"].rsplit(".", 1)[-1],
            "MaxResults": 1000,
        },
        body,
    )


def test_recorded_table_columns_are_read_as_recorded(aws_env, monkeypatch, clock):
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps() + [_describe_table("real_table_columns.json")],
    )
    columns = fake.adapter(clock=clock).table_columns(
        ("live_check", "events"), fake.deadline(clock, 30)
    )
    script.assert_played()
    assert columns == (
        ("user_id", "varchar"),
        ("event_at", "timestamp"),
        ("amount", "float8"),
        ("kind", "varchar"),
    )


def test_a_recorded_missing_table_has_no_columns_and_is_not_found(
    aws_env, monkeypatch, clock
):
    """DescribeTable answers 200 with an empty column list for a missing table."""
    script = _script(
        monkeypatch,
        clock,
        fake.assume_steps() + [_describe_table("real_missing_table.json")],
    )
    with pytest.raises(WarehouseError) as err:
        fake.adapter(clock=clock).table_columns(
            ("live_check", "experimently_live_check_probe"), fake.deadline(clock, 30)
        )
    script.assert_played()
    assert err.value.code is _C.OBJECT_NOT_FOUND
