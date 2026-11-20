"""``AuditService.audit_with_change`` and ``AuditWriteFailed`` (#1042).

A flag route writes its audit entries inside ``audit_with_change`` and commits
the change and the entries together. These pin the writer's contract with a
mocked session; ``backend/tests/integration/api/test_flag_change_audit_strict.py``
drives the routes on Postgres.
"""

from __future__ import annotations

import logging
import uuid
from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException
from sqlalchemy.exc import IntegrityError

from backend.app.core.metrics import audit_write_failures_total
from backend.app.models.audit_log import ActionType, EntityType
from backend.app.services.audit_service import AuditService, AuditWriteFailed

pytestmark = [pytest.mark.unit, pytest.mark.regression]

CANARY = "audit-with-change-canary"


def _block(db, entity_id=None):
    return AuditService.audit_with_change(
        db,
        action=ActionType.TOGGLE_ENABLE,
        entity_type=EntityType.FEATURE_FLAG,
        entity_id=entity_id or uuid.uuid4(),
    )


def test_audit_write_failed_is_never_a_refusal_of_the_request():
    """The routes answer a ValueError with 400 and its text, and an
    IntegrityError with 409 or 422; a failed entry must reach none of them."""
    assert issubclass(AuditWriteFailed, Exception)
    for refusal in (ValueError, IntegrityError, HTTPException):
        assert not issubclass(AuditWriteFailed, refusal), refusal


def test_its_text_names_the_action_the_entity_and_the_cause_type_only():
    entity_id = uuid.uuid4()
    error = AuditWriteFailed(
        ActionType.TOGGLE_ENABLE, EntityType.FEATURE_FLAG, entity_id, KeyError(CANARY)
    )
    assert str(error) == (
        f"audit entry for toggle_enable on feature_flag {entity_id} could not be "
        "written (KeyError)"
    )


def test_the_change_is_flushed_first_and_its_own_failure_is_raised_as_it_is():
    db = MagicMock()
    refused = IntegrityError("INSERT", {}, Exception(CANARY))
    db.flush.side_effect = [refused]
    entered = []
    with pytest.raises(IntegrityError) as raised:
        with _block(db):
            entered.append(True)
    assert raised.value is refused
    assert entered == []


@pytest.mark.parametrize("where", ["block", "flush"])
def test_a_failed_entry_is_audit_write_failed_from_none(where):
    """From the block or from the flush that writes the entries; the cause's
    text, which can carry the row, is in no traceback."""
    db = MagicMock()
    if where == "flush":
        db.flush.side_effect = [None, RuntimeError(CANARY)]
    with pytest.raises(AuditWriteFailed) as raised:
        with _block(db):
            if where == "block":
                raise ValueError(CANARY)
    error = raised.value
    assert CANARY not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__ is True


def test_it_never_commits_never_rolls_back_and_counts_no_lost_entry(caplog):
    """A refused change has lost no entry: the alarm's lines and counter stay
    as they were. The caller commits or rolls back."""
    db = MagicMock()
    db.flush.side_effect = [None, RuntimeError(CANARY)]
    failures = audit_write_failures_total._value.get()
    with caplog.at_level(logging.DEBUG, logger="backend.app.services.audit_service"):
        with pytest.raises(AuditWriteFailed):
            with _block(db):
                AuditService.record(
                    db,
                    actor=None,
                    action=ActionType.TOGGLE_ENABLE,
                    entity_type=EntityType.FEATURE_FLAG,
                    entity_id=uuid.uuid4(),
                    entity_name="New checkout",
                )
        with _block(MagicMock()):
            pass
    db.commit.assert_not_called()
    db.rollback.assert_not_called()
    assert audit_write_failures_total._value.get() == failures
    assert [r for r in caplog.records if r.levelno >= logging.ERROR] == []
