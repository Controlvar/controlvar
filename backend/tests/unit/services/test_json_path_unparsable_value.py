"""A json_path value that cannot be parsed does not match, and is not an error.

An experiment's ``json_path`` condition parses the user's attribute as JSON. A
value nested too deeply for the parser raises ``RecursionError``, which the
evaluator's last-resort handler used to log at ERROR and count, once per
evaluation, as if the rule were broken. The user's value was at fault, not the
rule, so the condition now does not match and nothing above DEBUG is logged:

* a value nested too deeply to parse (a ``[[[...]]]`` of 6,000 characters);
* a value longer than 64 KiB (``JSON_PATH_MAX_VALUE_LENGTH``), which is not parsed at
  all, and is shown here to be one that WOULD match if it were parsed.

Each is driven through the evaluator, through the rules service and through
``AssignmentService``, the path a request takes. No record carries the value.
"""

from __future__ import annotations

import json
import logging
from types import SimpleNamespace
from unittest.mock import MagicMock
from uuid import uuid4

import pytest

from backend.app.schemas.targeting_rule import TargetingRules
from backend.app.services import rules_evaluation_service
from backend.app.services.assignment_service import AssignmentService
from backend.app.services.rules_evaluation_service import RulesEvaluationService

pytestmark = pytest.mark.unit

DEPTH = 3000  # "[" * 3000 + "]" * 3000 is 6,000 characters

#: The longest value a json_path condition parses: 64 KiB, in characters. The
#: behaviour is pinned against this number, not against the service's constant,
#: so the tests say what the limit IS; the last test ties the two together.
LIMIT = 64 * 1024

#: A native ruleset: the condition matches when ``attr`` parses as JSON and its
#: ``a`` is "x". The name ``attr`` is the user-context key.
NATIVE_RULES = {
    "version": "1.0",
    "rules": [
        {
            "id": "r1",
            "name": "r1",
            "rule": {
                "operator": "and",
                "conditions": [
                    {
                        "attribute": "attr",
                        "operator": "json_path",
                        "value": "$.a",
                        "additional_value": "x",
                    }
                ],
                "groups": [],
            },
            "rollout_percentage": 100,
        }
    ],
}


def _too_deep() -> str:
    return "[" * DEPTH + "]" * DEPTH


def _document_of_length(length: int) -> str:
    """Valid JSON, exactly ``length`` characters, whose ``a`` is "x"."""
    head, tail = '{"a": "x", "pad": "', '"}'
    document = head + "p" * (length - len(head) - len(tail)) + tail
    assert len(document) == length
    return document


def _over_the_bound() -> str:
    return _document_of_length(LIMIT + 1)


def _at_the_bound() -> str:
    return _document_of_length(LIMIT)


def _evaluate(value: str) -> bool:
    return RulesEvaluationService()._evaluate_json_path(value, "$.a", "x")


def _rules() -> TargetingRules:
    return TargetingRules.model_validate(NATIVE_RULES)


def _loud(caplog) -> list:
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


def _quote_free(caplog, value: str) -> None:
    """No record repeats any part of the user's value."""
    needle = value[:40]
    assert [r.getMessage() for r in caplog.records if needle in r.getMessage()] == []


def test_the_premises_hold():
    """The values below are what they claim, on this interpreter."""
    too_deep = _too_deep()
    assert len(too_deep) == 6000
    with pytest.raises(RecursionError):
        json.loads(too_deep)

    # Valid JSON that matches, so only the bound keeps it from matching.
    over = _over_the_bound()
    assert len(over) > LIMIT
    assert json.loads(over)["a"] == "x"


@pytest.mark.regression
def test_a_json_path_value_too_deep_to_parse_does_not_match():
    """The evaluator answers False; the parser's RecursionError does not escape."""
    assert _evaluate(_too_deep()) is False


@pytest.mark.regression
def test_a_json_path_value_too_deep_to_parse_is_not_logged_as_an_error(caplog):
    """Through the rules service and AssignmentService: no ERROR record, no match."""
    value = _too_deep()
    with caplog.at_level(logging.DEBUG):
        service = RulesEvaluationService()
        matched, metrics = service.evaluate_rules_with_validation(
            _rules(), {"attr": value}, owner="experiment:exp-1"
        )
        verdict = AssignmentService(MagicMock())._evaluate_experiment_targeting(
            SimpleNamespace(id=uuid4(), targeting_rules=NATIVE_RULES),
            {"attr": value},
            user_id="u1",
        )

    assert _loud(caplog) == []
    assert caplog.records, "nothing was captured: the capture is not working"
    _quote_free(caplog, value)
    assert matched is None
    assert metrics.error is None
    assert dict(service.error_counts) == {}
    assert verdict["eligible"] is False
    assert verdict["reason"] == "No targeting rules matched"
    assert verdict["validation_passed"] is True


@pytest.mark.regression
def test_a_json_path_value_over_the_length_limit_does_not_match(caplog):
    value = _over_the_bound()
    with caplog.at_level(logging.DEBUG):
        assert _evaluate(value) is False

        service = RulesEvaluationService()
        matched, metrics = service.evaluate_rules_with_validation(
            _rules(), {"attr": value}, owner="experiment:exp-1"
        )
        assert matched is None
        assert metrics.error is None
        assert dict(service.error_counts) == {}

    assert _loud(caplog) == []
    assert caplog.records, "nothing was captured: the capture is not working"
    _quote_free(caplog, value)


def test_a_json_path_value_at_the_length_limit_still_matches():
    """The limit is inclusive: a document of exactly the limit is parsed."""
    assert _evaluate(_at_the_bound()) is True


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("{not json", id="not-json"),
        pytest.param("", id="empty"),
        pytest.param("1" * 5000, id="a-number-too-long-to-convert"),
        pytest.param('"x"', id="valid-json-that-is-not-a-container"),
    ],
)
def test_other_values_that_cannot_be_used_still_do_not_match(value, caplog):
    with caplog.at_level(logging.DEBUG):
        assert _evaluate(value) is False
    assert _loud(caplog) == []
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_a_document_that_parses_still_matches_and_is_still_compared():
    service = RulesEvaluationService()
    document = json.dumps({"a": "x", "b": {"c": [1, 2]}})
    assert service._evaluate_json_path(document, "$.a", "x") is True
    assert service._evaluate_json_path(document, "$.a", "y") is False
    assert service._evaluate_json_path(document, "$.b.c.1", 2) is True


def test_the_limit_is_the_constant_the_service_names():
    assert rules_evaluation_service.JSON_PATH_MAX_VALUE_LENGTH == LIMIT
