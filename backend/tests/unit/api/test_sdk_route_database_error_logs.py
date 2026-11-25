"""A database error on the SDK routes is logged by its class and SQLSTATE.

The driver's message can include the values of the statement that failed (a
constraint violation's DETAIL line names the key's values), and a traceback
prints that message. So each SDK route's generic failure path logs a database
error as ``"<operation> failed (<class>, SQLSTATE <code>)"``, with no
traceback, and answers exactly as before: the same status and the same fixed
sentence with the request ID. Any other error is still logged with its
traceback (the control).

The routes: ``/tracking/assign``, ``/tracking/track``, ``/tracking/events``,
an item of ``/tracking/batch``, ``/tracking/errors``, an item of
``/tracking/errors/batch`` and its commit, and the API key dependency's
last-used write, which every key route runs.

DB-free: through ``backend.app.main.app`` with a session that stores nothing,
and the step that would reach the database patched to raise an error shaped
like the driver's, carrying a made-up value where the driver's message can
carry one.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import uuid
from types import SimpleNamespace
from typing import Any, Callable, Dict, Iterator, List, NamedTuple, Optional

import pytest
import structlog
from fastapi.testclient import TestClient
from loguru import logger as loguru_logger
from sqlalchemy.exc import IntegrityError, ProgrammingError

from backend.app.api import deps
from backend.app.api.v1.endpoints import client_errors, tracking
from backend.app.main import app
from backend.app.models.api_key import APIKey
from backend.app.models.experiment import Experiment, ExperimentStatus
from backend.app.models.user import User
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.event_service import EventService
from backend.app.services.metrics_service import MetricsService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

#: The value the planted errors carry in the driver's message, and nowhere
#: else: it is not in any request.
PLANTED = "u-planted-7f3"
USER_ID = "u-request-2b1"
REQUEST_ID = "req-dberr-1"
HEADERS = {"X-API-Key": "k-dberr-not-a-key", "X-Request-ID": REQUEST_ID}
FLAG_ID = "00000000-0000-4000-8000-0000000000f1"
EXPERIMENT_KEY = "exp-dberr"


# ---------------------------------------------------------------------------
# Errors shaped like the driver's
# ---------------------------------------------------------------------------


class _DriverError(Exception):
    """The driver's own exception, as a log line sees it: its message, and
    the SQLSTATE, which psycopg2 calls ``pgcode``."""

    def __init__(self, message: str, pgcode: str) -> None:
        super().__init__(message)
        self.pgcode = pgcode


def _unique_violation() -> IntegrityError:
    return IntegrityError(
        "INSERT INTO events (user_id, event_type) VALUES (%(user_id)s, %(event_type)s)",
        {"user_id": PLANTED, "event_type": "click"},
        _DriverError(
            'duplicate key value violates unique constraint "uq_events_dedup"\n'
            f"DETAIL:  Key (user_id, dedup_key)=({PLANTED}, k-1) already exists.\n",
            "23505",
        ),
        hide_parameters=True,
    )


def _undefined_column() -> ProgrammingError:
    return ProgrammingError(
        "SELECT events.id FROM events WHERE events.user_id = %(user_id)s",
        {"user_id": PLANTED},
        _DriverError(
            "column events.dedup_key does not exist\n"
            f"LINE 1: ...FROM events WHERE events.user_id = '{PLANTED}' AND events...\n",
            "42703",
        ),
        hide_parameters=True,
    )


def _wrapped() -> RuntimeError:
    """Not a database error itself, but raised from one: a traceback prints
    the cause too."""
    wrapper = RuntimeError("could not finish the write")
    wrapper.__cause__ = _unique_violation()
    return wrapper


#: id -> (the error, what the log line says about it).
DATABASE_ERRORS: Dict[str, tuple[Callable[[], BaseException], str]] = {
    "unique-violation": (_unique_violation, "IntegrityError, SQLSTATE 23505"),
    "undefined-column": (_undefined_column, "ProgrammingError, SQLSTATE 42703"),
    "raised-from-one": (_wrapped, "RuntimeError from IntegrityError, SQLSTATE 23505"),
}


# ---------------------------------------------------------------------------
# A session that stores nothing
# ---------------------------------------------------------------------------


class _Query:
    """A query that finds ``found``, whatever it is chained with."""

    def __init__(self, found: Any = None) -> None:
        self._found = found

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: self

    def first(self) -> Any:
        return self._found

    def all(self) -> List[Any]:
        return []


class _Session:
    """``query(Model)`` finds ``found[Model]``; ``commit`` raises
    ``commit_error`` when there is one; everything else does nothing."""

    def __init__(
        self,
        found: Optional[Dict[type, Any]] = None,
        commit_error: Optional[BaseException] = None,
    ) -> None:
        self.found = found or {}
        self.commit_error = commit_error

    def query(self, model: Any, *args: Any, **kwargs: Any) -> _Query:
        return _Query(self.found.get(model))

    def commit(self) -> None:
        if self.commit_error is not None:
            raise self.commit_error

    def __getattr__(self, name: str) -> Any:
        # add, add_all, rollback, flush, refresh, close, connection.
        return lambda *args, **kwargs: None


@contextlib.contextmanager
def _client(session: _Session, *, admit_key: bool = True) -> Iterator[TestClient]:
    def get_db() -> Iterator[_Session]:
        yield session

    app.dependency_overrides[deps.get_db] = get_db
    if admit_key:
        app.dependency_overrides[deps.get_api_key] = lambda: None
    try:
        yield TestClient(app, raise_server_exceptions=False)
    finally:
        app.dependency_overrides.clear()


def _raising(error: BaseException) -> Callable[..., Any]:
    def fail(*args: Any, **kwargs: Any) -> Any:
        raise error

    return fail


# ---------------------------------------------------------------------------
# One case per generic failure path: how to reach it, and what it answers
# ---------------------------------------------------------------------------


def _choose_nobody(user_id: str) -> Optional[uuid.UUID]:
    """A fixed-allocation experiment's bandit choice: none."""
    return None


def _fail_assign(monkeypatch, error: BaseException) -> _Session:
    experiment = Experiment(
        id=uuid.uuid4(),
        key=EXPERIMENT_KEY,
        name="Database errors",
        status=ExperimentStatus.ACTIVE,
    )
    monkeypatch.setattr(tracking, "bandit_chooser", lambda db, exp: _choose_nobody)
    monkeypatch.setattr(AssignmentService, "assign_user", _raising(error))
    return _Session({Experiment: experiment})


def _fail_store(monkeypatch, error: BaseException) -> _Session:
    monkeypatch.setattr(EventService, "unknown_id_fields", lambda self, data: [])
    monkeypatch.setattr(EventService, "track_event_once", _raising(error))
    return _Session()


def _report_targets(monkeypatch) -> None:
    monkeypatch.setattr(client_errors, "report_source", lambda db, key: "client")
    monkeypatch.setattr(
        client_errors, "_resolve_targets", lambda db, report: (uuid.UUID(FLAG_ID), None)
    )


def _fail_report(monkeypatch, error: BaseException) -> _Session:
    _report_targets(monkeypatch)
    monkeypatch.setattr(MetricsService, "log_error", _raising(error))
    return _Session()


def _fail_report_item(monkeypatch, error: BaseException) -> _Session:
    monkeypatch.setattr(client_errors, "report_source", lambda db, key: "client")
    monkeypatch.setattr(client_errors, "_resolve_targets", _raising(error))
    return _Session()


def _fail_report_commit(monkeypatch, error: BaseException) -> _Session:
    _report_targets(monkeypatch)
    return _Session(commit_error=error)


def _detail(resp) -> str:
    return resp.json()["detail"]


def _first_item_error(resp) -> str:
    return resp.json()["errors"][0]["error"]


REPORT = {
    "feature_flag_key": "flag-dberr",
    "user_id": USER_ID,
    "error_type": "crash",
    "message": "m",
}


class _Route(NamedTuple):
    id: str
    url: str
    body: Dict[str, Any]
    fail: Callable[..., _Session]
    status: int
    answer: Callable[[Any], str]
    sentence: str
    logger: str
    operation: str


TRACKING = tracking.logger.name
CLIENT_ERRORS = client_errors.logger.name

ROUTES = [
    _Route(
        "assign",
        "/api/v1/tracking/assign",
        {"experiment_key": EXPERIMENT_KEY, "user_id": USER_ID},
        _fail_assign,
        500,
        _detail,
        "Could not assign the user to the experiment",
        TRACKING,
        "Tracking assign",
    ),
    _Route(
        "track",
        "/api/v1/tracking/track",
        {"event_type": "click", "user_id": USER_ID},
        _fail_store,
        500,
        _detail,
        "Could not store the event",
        TRACKING,
        "Tracking event",
    ),
    _Route(
        "events",
        "/api/v1/tracking/events",
        {
            "event_type": "click",
            "event_name": "click",
            "user_id": USER_ID,
            "feature_flag_id": FLAG_ID,
        },
        _fail_store,
        500,
        _detail,
        "Could not store the event",
        TRACKING,
        "Tracking event",
    ),
    _Route(
        "batch-item",
        "/api/v1/tracking/batch",
        {"events": [{"event_type": "click", "user_id": USER_ID}]},
        _fail_store,
        200,
        _first_item_error,
        "Could not store this event",
        TRACKING,
        "Tracking batch item",
    ),
    _Route(
        "errors",
        "/api/v1/tracking/errors",
        REPORT,
        _fail_report,
        500,
        _detail,
        "Could not store the error report",
        CLIENT_ERRORS,
        "Error report",
    ),
    _Route(
        "errors-batch-item",
        "/api/v1/tracking/errors/batch",
        {"errors": [REPORT]},
        _fail_report_item,
        200,
        _first_item_error,
        "Could not store this error report",
        CLIENT_ERRORS,
        "Error report batch item",
    ),
    _Route(
        "errors-batch-whole",
        "/api/v1/tracking/errors/batch",
        {"errors": [REPORT]},
        _fail_report_commit,
        500,
        _detail,
        "Could not store the error reports",
        CLIENT_ERRORS,
        "Error report batch",
    ),
]


def _everything_in(record: logging.LogRecord) -> str:
    """All a handler could write for *record*: the message, its arguments,
    the traceback and every attribute."""
    parts = [record.getMessage(), repr(record.args), record.exc_text or ""]
    if record.exc_info:
        parts.append(logging.Formatter().formatException(record.exc_info))
    parts.append(repr(record.__dict__))
    return "\n".join(parts)


def _post(route: _Route, session: _Session):
    with _client(session) as client:
        return client.post(route.url, json=route.body, headers=HEADERS)


def _failures(caplog, route: _Route) -> List[logging.LogRecord]:
    return [
        r
        for r in caplog.records
        if r.name == route.logger and r.levelno == logging.ERROR
    ]


# ---------------------------------------------------------------------------
# The routes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("error_id", list(DATABASE_ERRORS))
@pytest.mark.parametrize("route", ROUTES, ids=[r.id for r in ROUTES])
def test_a_database_error_is_logged_by_its_class_and_sqlstate_only(
    route: _Route, error_id: str, monkeypatch, caplog
) -> None:
    make_error, label = DATABASE_ERRORS[error_id]
    session = route.fail(monkeypatch, make_error())
    caplog.set_level(logging.DEBUG)

    resp = _post(route, session)

    # The answer is unchanged.
    assert resp.status_code == route.status, resp.text
    assert route.answer(resp) == f"{route.sentence} (request ID: {REQUEST_ID})."
    assert PLANTED not in resp.text
    # The value is in no record at all.
    assert caplog.records, "nothing was captured, so nothing was checked"
    for record in caplog.records:
        assert PLANTED not in _everything_in(record), record.getMessage()
    assert PLANTED not in caplog.text
    # One line, by class and SQLSTATE, with no traceback, from the route's
    # own module.
    failures = _failures(caplog, route)
    assert [r.getMessage() for r in failures] == [f"{route.operation} failed ({label})"]
    assert failures[0].exc_info is None
    assert failures[0].module == route.logger.rsplit(".", 1)[-1]


@pytest.mark.parametrize("route", ROUTES, ids=[r.id for r in ROUTES])
def test_any_other_error_is_still_logged_with_its_traceback(
    route: _Route, monkeypatch, caplog
) -> None:
    """The control: an error with no database error in its chain is logged as
    before, with its traceback, which prints its text."""
    error = RuntimeError(f"not a database error: {PLANTED}")
    session = route.fail(monkeypatch, error)
    caplog.set_level(logging.DEBUG)

    resp = _post(route, session)

    assert resp.status_code == route.status, resp.text
    assert route.answer(resp) == f"{route.sentence} (request ID: {REQUEST_ID})."
    assert PLANTED not in resp.text
    failures = _failures(caplog, route)
    assert [r.getMessage() for r in failures] == [
        f"{route.operation} failed (RuntimeError)"
    ]
    assert failures[0].exc_info is not None and failures[0].exc_info[1] is error
    assert failures[0].module == route.logger.rsplit(".", 1)[-1]
    assert PLANTED in caplog.text


# ---------------------------------------------------------------------------
# The line production writes
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _json_logs() -> Iterator[io.StringIO]:
    """Render every log record as the production JSON line, into a buffer."""
    from backend.app.core.logger import configure_logging

    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    saved = structlog.get_config()
    buffer = io.StringIO()
    configure_logging(log_level="INFO", json_logs=True, stream=buffer)
    try:
        yield buffer
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in handlers:
            root.addHandler(handler)
        root.setLevel(level)
        structlog.configure(**saved)


def test_the_json_line_carries_the_request_id_and_not_the_value(monkeypatch) -> None:
    route = next(r for r in ROUTES if r.id == "track")
    session = route.fail(monkeypatch, _unique_violation())

    with _json_logs() as buffer:
        resp = _post(route, session)

    assert resp.status_code == 500, resp.text
    assert PLANTED not in buffer.getvalue()
    lines = []
    for raw in buffer.getvalue().splitlines():
        try:
            lines.append(json.loads(raw))
        except ValueError:
            continue
    failures = [
        line
        for line in lines
        if line.get("event") == "Tracking event failed (IntegrityError, SQLSTATE 23505)"
    ]
    assert len(failures) == 1, buffer.getvalue()[-3000:]
    assert failures[0]["request_id"] == REQUEST_ID
    assert failures[0]["level"] == "error"
    assert "exception" not in failures[0]


# ---------------------------------------------------------------------------
# The API key dependency, on every key route
# ---------------------------------------------------------------------------


def test_a_failed_last_used_write_is_logged_by_its_class_and_sqlstate_only(
    caplog,
) -> None:
    key_id = uuid.uuid4()
    key = SimpleNamespace(
        id=key_id,
        is_valid=True,
        user_id=uuid.uuid4(),
        environment="production",
        update_last_used=_raising(_undefined_column()),
    )
    session = _Session({APIKey: key, User: SimpleNamespace(is_active=True)})
    # deps.py logs with loguru, which caplog does not see.
    lines: List[str] = []
    sink = loguru_logger.add(lambda message: lines.append(str(message)), level="TRACE")
    caplog.set_level(logging.DEBUG)
    try:
        with _client(session, admit_key=False) as client:
            resp = client.get(f"/api/v1/feature-flags/user/{USER_ID}", headers=HEADERS)
    finally:
        loguru_logger.remove(sink)

    # A failed write never fails the request.
    assert resp.status_code == 200, resp.text
    assert resp.json() == {}
    naming = [line for line in lines if "Could not record API key use" in line]
    assert len(naming) == 1, lines
    for line in lines:
        assert PLANTED not in line, line
    assert PLANTED not in caplog.text
    assert "WARNING" in naming[0]
    assert (
        f"Could not record API key use for key {key_id} "
        "(ProgrammingError, SQLSTATE 42703)"
    ) in naming[0]
