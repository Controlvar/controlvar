"""Service results built from a caught error carry its type, never its text.

These results (an evaluation's ``error``, a validation issue's ``message``, a
rollback's ``message``, an eligibility ``reason``) are handed to routes and
callers; the error's text can repeat a submitted value or a database row.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from backend.app.core.logger import bind_log_context, failure_detail
from backend.app.core.rule_validation import RuleValidator
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.rules_evaluation_service import RulesEvaluationService
from backend.app.services.safety_service import SafetyService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

CANARY = "CANARY zq540 value=secret-attribute"


def _boom(*args: Any, **kwargs: Any) -> Any:
    raise RuntimeError(CANARY)


class _RaisesOnCompare:
    def __eq__(self, other: object) -> bool:
        raise RuntimeError(CANARY)

    __hash__ = object.__hash__


def test_an_evaluation_error_carries_the_type_only(monkeypatch):
    service = RulesEvaluationService()
    monkeypatch.setattr(service, "_compile_rules", _boom)
    result = service.evaluate(MagicMock(), {}, skip_cache=True)
    assert result.matched is False
    assert result.error == "Evaluation failed (RuntimeError)"


def test_a_validated_evaluation_error_carries_the_type_only(monkeypatch):
    service = RulesEvaluationService()
    monkeypatch.setattr(service, "_evaluate_targeting_rules_enhanced", _boom)
    _, metrics = service.evaluate_rules_with_validation(
        MagicMock(), {}, track_metrics=True
    )
    assert metrics.error == "Evaluation failed (RuntimeError)"


def test_a_rule_validation_error_carries_the_type_only(monkeypatch):
    validator = RuleValidator()
    monkeypatch.setattr(validator, "_validate_schema_structure", _boom)
    result = validator.validate_targeting_rules(MagicMock())
    assert not result.is_valid
    assert [i.message for i in result.issues] == ["Validation failed (RuntimeError)"]


def test_an_operator_validation_error_carries_the_type_only():
    condition = SimpleNamespace(
        operator=_RaisesOnCompare(), value="v", additional_value=None
    )
    issues = RuleValidator()._validate_operator_compatibility(condition, "r1", "p")
    assert [i.message for i in issues] == ["Validation error (RuntimeError)"]


def test_a_targeting_evaluation_error_is_a_fixed_reason(monkeypatch):
    service = AssignmentService(MagicMock())
    monkeypatch.setattr(service, "_coerce_targeting_rules", _boom)
    result = service._evaluate_experiment_targeting(
        SimpleNamespace(id=uuid4(), targeting_rules={"rules": []}), {}
    )
    assert result["eligible"] is False
    assert result["reason"] == "Targeting evaluation error"


def test_a_failed_rollback_answers_the_fixed_message():
    bind_log_context(request_id="rollback-540")
    db = MagicMock()
    db.query.side_effect = RuntimeError(CANARY)
    response = SafetyService(db).execute_rollback(db, uuid4())
    assert response.success is False
    assert response.message == failure_detail("Rollback failed")
    assert response.message == "Rollback failed (request ID: rollback-540)."
    db.rollback.assert_called()
