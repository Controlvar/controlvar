"""Upstream error text never reaches a response, a raised message or a log record."""

from __future__ import annotations

import json
import logging
import traceback

import pytest

from modules.backend.app.warehouse.deadlines import Deadline
from modules.backend.app.warehouse.egress import OutboundClient
from modules.backend.app.warehouse.errors import (
    MESSAGES,
    WarehouseError,
    WarehouseErrorCode,
    error_for_status,
)
from modules.backend.app.warehouse.executor import JobKind, WarehouseExecutor
from modules.backend.tests.unit.warehouse.conftest import (
    FakeBackend,
    http_answer,
    public_resolver,
)

SENTINEL = "UPSTREAM-TEXT-7f3a9c"

BIGQUERY_CODES = {"accessDenied": WarehouseErrorCode.PERMISSION_DENIED}


def _vendor_error_body() -> bytes:
    return json.dumps(
        {
            "error": {
                "code": 400,
                "message": f"Syntax error near {SENTINEL} at [1:8]",
                "status": "INVALID_ARGUMENT",
                "errors": [
                    {
                        "reason": "invalidQuery",
                        "message": f"Unrecognized name: {SENTINEL}",
                        "location": SENTINEL,
                    }
                ],
            }
        }
    ).encode()


def _adapter_call(deadline: Deadline, backend: FakeBackend):
    """What an adapter does: read the structured code only, map it, raise coded."""
    with OutboundClient(
        deadline,
        warehouse="bigquery",
        resolver=public_resolver(),
        network_backend=backend,
    ) as client:
        response = client.request(
            "POST", "https://bigquery.googleapis.com/bigquery/v2/projects/p/jobs"
        )
    if response.status_code >= 400:
        payload = response.json()
        reason = payload["error"]["errors"][0]["reason"]
        raise error_for_status(
            response.status_code,
            warehouse="bigquery",
            vendor_code=reason,
            request_id=response.headers.get("x-request-id"),
            codes=BIGQUERY_CODES,
        )
    return response


def _driver_raises(deadline: Deadline):
    raise RuntimeError(f"driver failure: {SENTINEL}")


def _adapter_raises_while_handling(deadline: Deadline):
    try:
        raise ValueError(f"vendor said {SENTINEL}")
    except ValueError:
        # An adapter that maps inside the handler: the ValueError becomes the
        # context of what it raises, and would print in a traceback.
        raise WarehouseError(
            WarehouseErrorCode.RESULT_INVALID, warehouse="snowflake"
        ) from None


def _formatted_records(records) -> str:
    formatter = logging.Formatter("%(levelname)s %(name)s %(message)s")
    out = []
    for record in records:
        out.append(formatter.format(record))
        out.append(repr(record.__dict__))
        if record.exc_info:
            out.append("".join(traceback.format_exception(*record.exc_info)))
    return "\n".join(out)


@pytest.mark.parametrize(
    "case",
    ["vendor_error_body", "driver_exception_text", "mapped_inside_handler"],
)
def test_upstream_error_text_absent_from_responses_and_logs(case, caplog):
    caplog.set_level(logging.DEBUG)
    backend = FakeBackend(
        [
            http_answer(
                400,
                _vendor_error_body(),
                headers=[
                    ("Content-Type", "application/json"),
                    ("X-Request-Id", "req-123"),
                ],
            )
        ]
    )
    jobs = {
        "vendor_error_body": lambda deadline: _adapter_call(deadline, backend),
        "driver_exception_text": _driver_raises,
        "mapped_inside_handler": _adapter_raises_while_handling,
    }
    executor = WarehouseExecutor(1, per_organisation=1)
    future = executor.try_submit(
        jobs[case], total_seconds=30, kind=JobKind.CONNECTION_TEST
    )
    try:
        with pytest.raises(WarehouseError) as caught:
            future.result(timeout=30)
    finally:
        executor.shutdown(wait=True)
    err = caught.value

    # What a route answers with, and everything a log line could print.
    surfaces = [
        str(err),
        repr(err),
        err.message,
        json.dumps(err.to_body()),
        json.dumps(err.response_headers()),
        json.dumps(err.log_fields()),
        "".join(traceback.format_exception(err)),
        caplog.text,
        _formatted_records(caplog.records),
    ]
    for surface in surfaces:
        assert SENTINEL not in surface
    assert err.__context__ is None and err.__cause__ is None

    # Positive controls: the failure was logged, with our code, so the log
    # assertion above looked at real records.
    codes = [getattr(r, "warehouse_error", None) for r in caplog.records]
    assert err.code.value in codes
    if case == "vendor_error_body":
        assert err.code is WarehouseErrorCode.UNRECOGNISED_WAREHOUSE_ERROR
        assert err.vendor_code == "invalidQuery"
        assert err.http_status == 400
        assert err.request_id == "req-123"
        assert err.to_body() == {
            "code": "unrecognised_warehouse_error",
            "message": (
                "BigQuery returned an error we don't recognise (code invalidQuery). "
                "See Troubleshooting in the warehouse analysis docs."
            ),
            "vendor_code": "invalidQuery",
        }
    elif case == "driver_exception_text":
        assert err.code is WarehouseErrorCode.INTERNAL
        assert "RuntimeError" in caplog.text
    else:
        assert err.code is WarehouseErrorCode.RESULT_INVALID


@pytest.mark.parametrize(
    "value",
    [
        f"bad code {SENTINEL}",
        SENTINEL + " x",
        "a" * 65,
        "x\ny",
        "<b>",
        3.5,
        True,
        {"a": 1},
    ],
)
def test_structured_fields_that_are_not_short_tokens_are_dropped(value):
    err = WarehouseError(
        WarehouseErrorCode.UNRECOGNISED_WAREHOUSE_ERROR,
        vendor_code=value,
        request_id=value,
    )
    assert err.vendor_code is None and err.request_id is None
    assert "none" in err.message


def test_mapped_vendor_code_wins_over_status():
    err = error_for_status(
        400, warehouse="bigquery", vendor_code="accessDenied", codes=BIGQUERY_CODES
    )
    assert err.code is WarehouseErrorCode.PERMISSION_DENIED


@pytest.mark.parametrize(
    "status,code",
    [
        (401, WarehouseErrorCode.AUTH_FAILED),
        (403, WarehouseErrorCode.PERMISSION_DENIED),
        (404, WarehouseErrorCode.OBJECT_NOT_FOUND),
        (504, WarehouseErrorCode.TIME_LIMIT),
        (500, WarehouseErrorCode.UNRECOGNISED_WAREHOUSE_ERROR),
    ],
)
def test_status_fallback(status, code):
    assert error_for_status(status).code is code


def test_every_code_has_copy_and_admission_statuses():
    assert set(MESSAGES) == set(WarehouseErrorCode)
    busy = WarehouseError(WarehouseErrorCode.WAREHOUSE_BUSY)
    assert busy.status_code == 429
    assert busy.response_headers() == {"Retry-After": "30"}
    assert WarehouseError(WarehouseErrorCode.RUN_IN_PROGRESS).status_code == 409


def test_error_has_no_free_text_constructor():
    with pytest.raises(TypeError):
        WarehouseError(WarehouseErrorCode.INTERNAL, "free text")  # type: ignore[misc]
    with pytest.raises(ValueError):
        WarehouseError("not a code")  # type: ignore[arg-type]
