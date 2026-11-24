"""``failure_label`` and ``traceback_unless_database`` (``backend/app/core/logger.py``).

A database error is named by its class and SQLSTATE, never its text, and
logged with no traceback; the routes that use them are in
``backend/tests/unit/api/test_sdk_route_database_error_logs.py``. Anything
else is logged with its traceback, as ``logger.exception`` logs it.
"""

from __future__ import annotations

import logging

import psycopg2
import pytest
from sqlalchemy.exc import IntegrityError, OperationalError, PendingRollbackError

from backend.app.core.logger import (
    database_error_in,
    failure_label,
    sqlstate_of,
    traceback_unless_database,
)

pytestmark = [pytest.mark.unit, pytest.mark.regression]

PLANTED = "u-planted-7f3"
LOGGER = "test.failure_label"


class _DriverError(Exception):
    def __init__(self, message: str, pgcode: object) -> None:
        super().__init__(message)
        self.pgcode = pgcode


def _integrity(pgcode: object = "23505") -> IntegrityError:
    return IntegrityError(
        "INSERT INTO events (user_id) VALUES (%(user_id)s)",
        {"user_id": PLANTED},
        _DriverError(f"DETAIL:  Key (user_id)=({PLANTED}) already exists.", pgcode),
        hide_parameters=True,
    )


class _RawUniqueViolation(psycopg2.errors.UniqueViolation):
    """psycopg2's own error, not wrapped by SQLAlchemy. psycopg2 sets
    ``pgcode`` from the server's answer; this one has it as a class value."""

    pgcode = "23505"


def _raised(error: BaseException) -> BaseException:
    """*error* after it was raised, so it has a traceback."""
    try:
        raise error
    except BaseException as caught:
        return caught


def _logged(caplog, exc: BaseException) -> logging.LogRecord:
    """*exc* logged as the routes' generic ``except`` blocks log it."""
    caplog.set_level(logging.DEBUG, logger=LOGGER)
    logging.getLogger(LOGGER).error(
        "Probe failed (%s)", failure_label(exc), exc_info=traceback_unless_database(exc)
    )
    (record,) = [r for r in caplog.records if r.name == LOGGER]
    return record


def _everything_in(record: logging.LogRecord) -> str:
    parts = [record.getMessage(), repr(record.args), repr(record.__dict__)]
    if record.exc_info:
        parts.append(logging.Formatter().formatException(record.exc_info))
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# failure_label
# ---------------------------------------------------------------------------


def test_a_driver_error_wrapped_by_sqlalchemy_is_its_class_and_sqlstate() -> None:
    assert failure_label(_integrity()) == "IntegrityError, SQLSTATE 23505"


def test_psycopg2_s_own_error_is_its_class_and_sqlstate() -> None:
    error = _RawUniqueViolation(f"DETAIL:  Key (user_id)=({PLANTED}) already exists.")
    assert database_error_in(error) is error
    assert failure_label(error) == "_RawUniqueViolation, SQLSTATE 23505"


def test_a_database_error_further_down_the_chain_is_named_after_the_class() -> None:
    by_cause = RuntimeError("wrapper")
    by_cause.__cause__ = _integrity()
    assert failure_label(by_cause) == "RuntimeError from IntegrityError, SQLSTATE 23505"

    try:
        try:
            raise _integrity()
        except IntegrityError:
            raise KeyError("while handling it")
    except KeyError as by_context:
        assert failure_label(by_context) == (
            "KeyError from IntegrityError, SQLSTATE 23505"
        )


def test_a_long_chain_is_followed_to_its_end_and_a_cycle_ends() -> None:
    deepest = _integrity()
    top: BaseException = deepest
    for depth in range(50):
        wrapper = RuntimeError(f"level {depth}")
        wrapper.__cause__ = top
        top = wrapper
    assert database_error_in(top) is deepest

    first, second = RuntimeError("a"), RuntimeError("b")
    first.__cause__, second.__cause__ = second, first
    assert database_error_in(first) is None
    assert failure_label(first) == "RuntimeError"


@pytest.mark.parametrize(
    "pgcode", [None, "", "2350", "235051", "23505 ", "2350a", 23505, PLANTED]
)
def test_a_sqlstate_that_is_not_five_digits_or_capitals_is_left_out(pgcode) -> None:
    error = _integrity(pgcode)
    assert sqlstate_of(error) is None
    assert failure_label(error) == "IntegrityError"


def test_a_sqlstate_that_cannot_be_read_is_left_out() -> None:
    class _Unreadable(Exception):
        @property
        def pgcode(self) -> str:
            raise RuntimeError(PLANTED)

    error = OperationalError("SELECT 1", {}, _Unreadable("gone"))
    assert sqlstate_of(error) is None
    assert failure_label(error) == "OperationalError"


def test_a_sqlalchemy_error_with_no_driver_error_is_its_class() -> None:
    error = PendingRollbackError(f"Original exception was: Key ({PLANTED})")
    assert database_error_in(error) is error
    assert failure_label(error) == "PendingRollbackError"


def test_any_other_error_is_its_class_name() -> None:
    assert database_error_in(ValueError(PLANTED)) is None
    assert failure_label(ValueError(PLANTED)) == "ValueError"


# ---------------------------------------------------------------------------
# traceback_unless_database, as a generic except block uses it
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "error,label",
    [
        (_integrity(), "IntegrityError, SQLSTATE 23505"),
        (
            _RawUniqueViolation(f"Key ({PLANTED})"),
            "_RawUniqueViolation, SQLSTATE 23505",
        ),
        (PendingRollbackError(f"Key ({PLANTED})"), "PendingRollbackError"),
    ],
    ids=["sqlalchemy", "psycopg2", "no-driver-error"],
)
def test_a_database_error_is_logged_without_its_text_or_traceback(
    caplog, error, label
) -> None:
    error = _raised(error)
    assert traceback_unless_database(error) is None
    record = _logged(caplog, error)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == f"Probe failed ({label})"
    assert record.exc_info is None
    assert PLANTED not in _everything_in(record)


def test_any_other_error_is_logged_with_its_traceback(caplog) -> None:
    error = _raised(ValueError(PLANTED))
    assert traceback_unless_database(error) is error
    record = _logged(caplog, error)
    assert record.levelno == logging.ERROR
    assert record.getMessage() == "Probe failed (ValueError)"
    assert record.exc_info is not None and record.exc_info[1] is error
    assert PLANTED in _everything_in(record)
