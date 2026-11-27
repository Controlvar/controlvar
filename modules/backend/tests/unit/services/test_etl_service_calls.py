"""ETLService makes only the Glue calls the API task role allows (#487).

The Fargate stack grants the full-profile API task six Glue actions on this
environment's jobs, crawler and catalog objects, and no others
(``infrastructure/cdk/stacks/fargate_service_stack.py``). A service method that
starts making any other call -- ``get_partitions``, ``get_tables``, a call
through a client held in a local variable -- would pass every moto test and be
refused in a deployment. So every public method is driven here, down every
branch that reaches Glue, against a fake that records every attribute reached on
the client, and any operation outside the six fails.

The same file pins where the client's region comes from: botocore reads
``AWS_DEFAULT_REGION`` (never ``AWS_REGION``), and ``settings.AWS_REGION`` is
not consulted; with no region at all, the routes answer a handled 500.
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from unittest.mock import MagicMock

import botocore.session
import pytest
from botocore import xform_name
from botocore.exceptions import ClientError, NoRegionError
from fastapi import HTTPException

from backend.app.core.logger import failure_detail
from modules.backend.app.schemas.etl import ETLJobRequest, ETLJobType
from modules.backend.app.services import etl_service
from modules.backend.app.services.etl_service import ETLService

pytestmark = pytest.mark.unit

JOB = "experimentation-events-etl-test"
METRICS_JOB = "experimentation-metrics-etl-test"
DATABASE = "experimentation_test"
TABLE = "raw_events"
CRAWLER = "experimentation-crawler-test"

#: The six operations the task role grants, as botocore's client names them.
ALLOWED = {
    "start_job_run",
    "get_job_run",
    "get_table",
    "batch_create_partition",
    "start_crawler",
    "get_crawler",
}
#: Attributes of a client that are not operations, so need no grant.
NOT_OPERATIONS = {"exceptions"}

_RESPONSES = {
    "start_job_run": {"JobRunId": "jr_1"},
    "get_job_run": {"JobRun": {"JobRunState": "SUCCEEDED"}},
    "get_table": {"Table": {"StorageDescriptor": {"Location": "s3://lake/raw/"}}},
    "batch_create_partition": {},
    "start_crawler": {},
    "get_crawler": {"Crawler": {"State": "READY"}},
}


def _client_error(code: str, operation: str) -> ClientError:
    """A ClientError whose class is named ``code``, as botocore's modelled ones are."""
    cls = type(code, (ClientError,), {})
    return cls({"Error": {"Code": code, "Message": "no"}}, operation)


class _Exceptions:
    """``client.exceptions``: any modelled exception class, by name."""

    def __getattr__(self, name: str):
        if name.startswith("__"):
            raise AttributeError(name)
        return type(name, (ClientError,), {})


def _glue_operation_names() -> frozenset[str]:
    """Every Glue client method, from botocore's offline service model."""
    model = botocore.session.get_session().get_service_model("glue")
    return frozenset(xform_name(op) for op in model.operation_names)


#: What a Glue client has: the operations, and ``exceptions``.
_CLIENT_ATTRIBUTES = _glue_operation_names() | NOT_OPERATIONS


class _Glue:
    """Records every operation reached on it.

    Only what a real Glue client has is recorded; any other name raises
    ``AttributeError`` as a real client would (so a traceback renderer probing
    the object is not mistaken for a Glue call).

    ``fail`` maps an operation to the error code its next call raises, so the
    callers' error branches run under the recorder too. A failed call is still
    recorded: it is a call the task role has to allow. ``reply`` maps an
    operation to the reply its next call returns in place of ``_RESPONSES``
    (a ``batch_create_partition`` reply carrying ``Errors``, a table with no
    location).
    """

    def __init__(self, calls: list):
        self._calls = calls
        self.fail: dict[str, str] = {}
        self.reply: dict[str, dict] = {}

    def __getattr__(self, attribute: str):
        if attribute not in _CLIENT_ATTRIBUTES:
            raise AttributeError(attribute)
        self._calls.append(attribute)
        if attribute == "exceptions":
            return _Exceptions()

        def operation(**kwargs):
            if attribute in self.fail:
                raise _client_error(self.fail.pop(attribute), attribute)
            if attribute in self.reply:
                return self.reply.pop(attribute)
            return _RESPONSES.get(attribute, {})

        return operation


@pytest.fixture
def configured(monkeypatch):
    for key, value in {
        "GLUE_ETL_JOB_NAME": JOB,
        "GLUE_METRICS_JOB_NAME": METRICS_JOB,
        "GLUE_DATABASE": DATABASE,
        "GLUE_EVENTS_TABLE": TABLE,
        "GLUE_CRAWLER_NAME": CRAWLER,
    }.items():
        monkeypatch.setattr(etl_service.modules_settings, key, value)


@pytest.fixture
def calls(monkeypatch, configured) -> list:
    """The calls made, with ``boto3.client`` replaced by the recorder."""
    recorded: list = []

    def client(service_name, *args, **kwargs):
        recorded.append(("boto3.client", service_name, tuple(args), tuple(kwargs)))
        return _Glue(recorded)

    monkeypatch.setattr(etl_service.boto3, "client", client)
    return recorded


def _drive(service: ETLService) -> dict:
    """Call every public method, down every happy branch that reaches Glue."""
    return {
        "run_etl_job": lambda: (
            [
                service.run_etl_job(ETLJobRequest(job_type=job_type, date="2026-10-01"))
                for job_type in ETLJobType
            ]
            + [
                service.run_etl_job(
                    ETLJobRequest(
                        job_type=ETLJobType.EVENTS_TO_PARQUET,
                        date="2026-10-01",
                        experiment_id="exp-1",
                    )
                )
            ]
        ),
        "get_job_status": lambda: service.get_job_status("jr_1", JOB),
        "add_partitions": lambda: (
            service.add_partitions(DATABASE, TABLE, "2026-10-01"),
            service.add_partitions(DATABASE, TABLE, "2026-10-01", "s3://lake/x/"),
        ),
        "run_crawler": lambda: service.run_crawler(),
        "get_crawler_status": lambda: service.get_crawler_status(CRAWLER),
    }


ALREADY_EXISTS = "AlreadyExistsException"
DENIED = "AccessDeniedException"


def _partition_errors(count: int, code: str) -> list[dict]:
    """``Errors`` as a successful BatchCreatePartition reply carries them."""
    return [
        {
            "PartitionValues": ["2026", "10", "01", f"{hour:02d}"],
            "ErrorDetail": {"ErrorCode": code, "ErrorMessage": "no"},
        }
        for hour in range(count)
    ]


def _raises(status_code: int, call) -> None:
    with pytest.raises(HTTPException) as caught:
        call()
    assert caught.value.status_code == status_code, caught.value


def _drive_failures(service: ETLService) -> dict:
    """Call every public method that handles an error, down each error branch.

    Each driver makes a Glue call fail, runs the method, and checks the error
    branch really ran -- so a call planted in an ``except`` block is reached
    and recorded, not skipped.
    """
    glue = service._glue()

    def run_etl_job():
        glue.fail["start_job_run"] = "ConcurrentRunsExceededException"
        _raises(
            500,
            lambda: service.run_etl_job(
                ETLJobRequest(job_type=ETLJobType.EVENTS_TO_PARQUET, date="2026-10-01")
            ),
        )

    def get_job_status():
        glue.fail["get_job_run"] = "EntityNotFoundException"
        _raises(404, lambda: service.get_job_status("jr_1", JOB))
        glue.fail["get_job_run"] = "OperationTimeoutException"
        _raises(500, lambda: service.get_job_status("jr_1", JOB))

    def add_partitions():
        def call():
            return service.add_partitions(DATABASE, TABLE, "2026-10-01")

        glue.fail["get_table"] = "AccessDeniedException"
        _raises(500, call)
        glue.fail["get_table"] = "EntityNotFoundException"
        _raises(404, call)
        glue.reply["get_table"] = {"Table": {"Name": TABLE}}
        _raises(500, call)
        glue.fail["batch_create_partition"] = "AlreadyExistsException"
        assert len(call()) == 24
        glue.fail["batch_create_partition"] = "AccessDeniedException"
        _raises(500, call)
        glue.reply["batch_create_partition"] = {"Errors": _partition_errors(1, DENIED)}
        _raises(500, call)
        glue.reply["batch_create_partition"] = {
            "Errors": _partition_errors(24, ALREADY_EXISTS)
        }
        assert len(call()) == 24

    def run_crawler():
        glue.fail["start_crawler"] = "CrawlerRunningException"
        _raises(409, lambda: service.run_crawler())
        glue.fail["start_crawler"] = "OperationTimeoutException"
        _raises(500, lambda: service.run_crawler())

    def get_crawler_status():
        glue.fail["get_crawler"] = "EntityNotFoundException"
        _raises(404, lambda: service.get_crawler_status())
        glue.fail["get_crawler"] = "OperationTimeoutException"
        _raises(500, lambda: service.get_crawler_status())

    return {
        "run_etl_job": run_etl_job,
        "get_job_status": get_job_status,
        "add_partitions": add_partitions,
        "run_crawler": run_crawler,
        "get_crawler_status": get_crawler_status,
    }


def _public_methods() -> set[str]:
    return {
        name
        for name, member in inspect.getmembers(ETLService, inspect.isfunction)
        if not name.startswith("_")
    }


def _methods_with_error_handling() -> set[str]:
    """The public methods whose source has a ``try`` statement."""
    found = set()
    for name in _public_methods():
        source = textwrap.dedent(inspect.getsource(getattr(ETLService, name)))
        if any(isinstance(node, ast.Try) for node in ast.walk(ast.parse(source))):
            found.add(name)
    return found


def _operations(made: list) -> set[str]:
    return {c for c in made if isinstance(c, str) and c not in NOT_OPERATIONS}


def _assert_allowed(calls: list, name: str, drive) -> None:
    before = len(calls)
    drive()
    made = calls[before:]
    assert not [c for c in made if not isinstance(c, str)], (
        f"{name} built another client: {made}"
    )
    operations = _operations(made)
    assert operations, f"{name} reached Glue not at all"
    assert operations <= ALLOWED, (
        f"{name} called {sorted(operations - ALLOWED)}; the API task role allows "
        f"only {sorted(ALLOWED)} (infrastructure/cdk/stacks/fargate_service_stack.py)"
    )


@pytest.mark.regression
def test_every_public_method_calls_only_the_granted_operations(calls):
    service = ETLService()
    service._glue()
    assert calls == [("boto3.client", "glue", (), ())], (
        "the Glue client is built once, as boto3.client('glue') with no region "
        f"or other argument; got {calls}"
    )

    drivers = _drive(service)
    assert _public_methods() == set(drivers), (
        f"public methods {sorted(_public_methods() - set(drivers))} are not "
        "driven here: add them, and make sure the task role grants what they call"
    )
    for name, drive in drivers.items():
        _assert_allowed(calls, name, drive)
    assert _operations(calls[1:]) == ALLOWED, (
        "every granted operation is reached by some method; an unused grant "
        f"is a grant to remove: {sorted(ALLOWED - _operations(calls[1:]))}"
    )


@pytest.mark.regression
def test_every_error_branch_calls_only_the_granted_operations(calls):
    """The except branches too: a happy-path drive never reaches them."""
    service = ETLService()
    drivers = _drive_failures(service)
    handling = _methods_with_error_handling()
    assert handling == set(drivers), (
        f"methods with error handling {sorted(handling - set(drivers))} are "
        "not driven down their error branch here"
    )
    for name, drive in drivers.items():
        _assert_allowed(calls, name, drive)
        assert service._glue().fail == {}, f"{name} never reached the failing call"
        assert service._glue().reply == {}, f"{name} never reached the planted reply"


def test_the_recorder_sees_a_call_outside_the_grant(calls):
    """Not vacuous: the fake records what the pin forbids, however it is reached."""
    service = ETLService()
    glue = service._glue()
    glue.get_partitions(DatabaseName=DATABASE, TableName=TABLE)
    service._glue().delete_job(JobName=JOB)
    assert {"get_partitions", "delete_job"} <= _operations(calls)
    assert not _operations(calls) <= ALLOWED


# --- the region ---------------------------------------------------------------


@pytest.fixture
def no_region_sources(monkeypatch):
    """No config file and no region variable: only what the test sets counts."""
    monkeypatch.setenv("AWS_CONFIG_FILE", "/dev/null")
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", "/dev/null")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.delenv("AWS_REGION", raising=False)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)


@pytest.mark.regression
def test_the_glue_client_takes_its_region_from_aws_default_region(
    monkeypatch, no_region_sources
):
    """#487: the client used settings.AWS_REGION (us-east-1 by default).

    The stacks deploy to us-west-2 and set AWS_DEFAULT_REGION on the task, so
    the API looked for its job and crawler in the wrong region. Building a
    client is offline: no Glue call is made.
    """
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-west-2")
    assert ETLService()._glue().meta.region_name == "us-west-2"


def test_with_no_region_the_glue_client_cannot_be_built(no_region_sources):
    """No silent default: outside the CDK, AWS_DEFAULT_REGION must be set."""
    with pytest.raises(NoRegionError):
        ETLService()._glue()


def test_the_glue_client_does_not_read_aws_region(monkeypatch, no_region_sources):
    """AWS_REGION, as a variable and as the setting, names no region for Glue."""
    from backend.app.core.config import settings

    monkeypatch.setattr(settings, "AWS_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_REGION", "eu-west-1")
    with pytest.raises(NoRegionError):
        ETLService()._glue()


@pytest.mark.parametrize(
    ("name", "call", "sentence"),
    [
        (
            "get_job_status",
            lambda s: s.get_job_status("jr_1", JOB),
            "Could not read the ETL job's status",
        ),
        (
            "run_etl_job",
            lambda s: s.run_etl_job(
                ETLJobRequest(job_type=ETLJobType.EVENTS_TO_PARQUET, date="2026-10-01")
            ),
            "Could not start the ETL job",
        ),
        ("run_crawler", lambda s: s.run_crawler(), "Could not start the crawler"),
        (
            "add_partitions",
            lambda s: s.add_partitions(DATABASE, TABLE, "2026-10-01"),
            "Could not read the Glue table",
        ),
        (
            "get_crawler_status",
            lambda s: s.get_crawler_status(),
            "Could not read the crawler's status",
        ),
    ],
)
def test_with_no_region_the_routes_answer_a_handled_500(
    monkeypatch, no_region_sources, configured, name, call, sentence
):
    """The fixed sentence, and the error's class name in the log.

    ``get_job_status`` used to name ``self._glue().exceptions`` in an
    ``except`` clause, which built the client a second time and let the
    ``NoRegionError`` escape the handler.
    """
    logger = MagicMock()
    monkeypatch.setattr(etl_service, "logger", logger)
    with pytest.raises(HTTPException) as caught:
        call(ETLService())
    assert caught.value.status_code == 500, (name, caught.value)
    assert caught.value.detail == failure_detail(sentence), caught.value.detail
    logged = [c.args for c in logger.error.call_args_list]
    assert any("NoRegionError" in args for args in logged), logged


# --- add_partitions: what a refusal reaches (#656) ---------------------------


def _after_the_client(calls: list) -> list:
    """The operations made, once the single ``boto3.client('glue')`` is built."""
    assert calls[:1] == [("boto3.client", "glue", (), ())], calls
    return calls[1:]


@pytest.mark.regression
@pytest.mark.parametrize(
    ("plant", "status_code"),
    [
        pytest.param({"fail": DENIED}, 500, id="get-table-refused"),
        pytest.param({"fail": "EntityNotFoundException"}, 404, id="table-not-found"),
        pytest.param({"reply": {"Table": {"Name": TABLE}}}, 500, id="no-descriptor"),
        pytest.param(
            {"reply": {"Table": {"Name": TABLE, "StorageDescriptor": {}}}},
            500,
            id="no-location",
        ),
        pytest.param(
            {"reply": {"Table": {"StorageDescriptor": {"Location": ""}}}},
            500,
            id="empty-location",
        ),
        pytest.param(
            {"reply": {"Table": {"StorageDescriptor": {"Location": None}}}},
            500,
            id="null-location",
        ),
    ],
)
def test_a_table_that_cannot_be_read_registers_nothing(calls, plant, status_code):
    """No usable location: GetTable is the only call, so no partition is made."""
    service = ETLService()
    glue = service._glue()
    if "fail" in plant:
        glue.fail["get_table"] = plant["fail"]
    else:
        glue.reply["get_table"] = plant["reply"]
    _raises(status_code, lambda: service.add_partitions(DATABASE, TABLE, "2026-10-01"))
    assert _after_the_client(calls) == ["get_table"]


@pytest.mark.parametrize(
    "date",
    ["2026-10", "2026-13-45", "20261001", "2026-1-01", "2026-W40-1", "2026-02-29", ""],
)
def test_a_date_that_is_not_yyyy_mm_dd_answers_422_before_any_client(calls, date):
    with pytest.raises(HTTPException) as caught:
        ETLService().add_partitions(DATABASE, TABLE, date)
    assert caught.value.status_code == 422, caught.value
    assert caught.value.detail == etl_service.INVALID_DATE_DETAIL
    assert calls == [], f"a client was built or Glue was called: {calls}"


def test_an_unconfigured_table_answers_404_before_any_client(calls):
    with pytest.raises(HTTPException) as caught:
        ETLService().add_partitions(DATABASE, "other_table", "2026-10-01")
    assert caught.value.status_code == 404, caught.value
    assert calls == [], f"a client was built or Glue was called: {calls}"


def test_the_partitions_come_from_the_parsed_date_under_the_table_location(calls):
    service = ETLService()
    partitions = service.add_partitions(DATABASE, TABLE, "2024-02-29")
    assert len(partitions) == 24
    assert {p.partition_values["hour"] for p in partitions} == {
        f"{h:02d}" for h in range(24)
    }
    first = partitions[0]
    assert first.partition_values == {
        "year": "2024",
        "month": "02",
        "day": "29",
        "hour": "00",
    }
    assert first.location == "s3://lake/raw/year=2024/month=02/day=29/hour=00/"
    assert _after_the_client(calls) == ["get_table", "batch_create_partition"]
